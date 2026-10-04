"""Unit tests for kidsplay_server.auth (password, sessions, tokens, secrets)."""

import stat
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import aiosqlite
import pytest

from kidsplay_server import auth
from kidsplay_server.auth import (
    TOKEN_PREFIX,
    LoginThrottle,
    check_admin_password,
    create_admin_token,
    create_session,
    delete_admin_token,
    delete_session,
    hash_password,
    init_auth_db,
    is_admin_configured,
    is_session_valid,
    list_admin_tokens,
    load_or_create_secret_key,
    set_initial_admin_password,
    verify_admin_token,
    verify_password,
)
from kidsplay_server.database import configure_conn


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(tmp_path / "auth.db") as c:
        await configure_conn(c)
        await init_auth_db(c)
        yield c


class TestSchema:
    async def test_init_auth_db_is_idempotent(self, conn: aiosqlite.Connection) -> None:
        await init_auth_db(conn)
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'admin_%' ORDER BY name"
        ) as cur:
            names = [r[0] for r in await cur.fetchall()]
        assert names == ["admin_account", "admin_sessions", "admin_tokens"]


class TestPasswordHashing:
    def test_hash_is_argon2id_and_salted(self) -> None:
        h1 = hash_password("secret-password")
        h2 = hash_password("secret-password")
        assert h1.startswith("$argon2id$")
        assert h1 != h2

    def test_verify_password(self) -> None:
        h = hash_password("secret-password")
        assert verify_password(h, "secret-password")
        assert not verify_password(h, "wrong-password")

    def test_verify_password_malformed_hash(self) -> None:
        assert not verify_password("not-a-hash", "anything")


class TestAdminAccount:
    async def test_not_configured_initially(self, conn: aiosqlite.Connection) -> None:
        assert not await is_admin_configured(conn)
        assert not await check_admin_password(conn, "anything")

    async def test_set_initial_password(self, conn: aiosqlite.Connection) -> None:
        assert await set_initial_admin_password(conn, "first-password")
        await conn.commit()
        assert await is_admin_configured(conn)
        assert await check_admin_password(conn, "first-password")
        assert not await check_admin_password(conn, "other-password")

    async def test_password_stored_hashed(self, conn: aiosqlite.Connection) -> None:
        await set_initial_admin_password(conn, "first-password")
        async with conn.execute("SELECT password_hash FROM admin_account") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert "first-password" not in row[0]
        assert row[0].startswith("$argon2id$")

    async def test_initial_password_never_overwritten(
        self, conn: aiosqlite.Connection
    ) -> None:
        assert await set_initial_admin_password(conn, "first-password")
        assert not await set_initial_admin_password(conn, "second-password")
        assert await check_admin_password(conn, "first-password")
        assert not await check_admin_password(conn, "second-password")

    async def test_outdated_hash_is_upgraded(
        self, conn: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from argon2 import PasswordHasher

        weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
        monkeypatch.setattr(auth, "_hasher", weak)
        await set_initial_admin_password(conn, "first-password")
        monkeypatch.undo()

        assert await check_admin_password(conn, "first-password")
        async with conn.execute("SELECT password_hash FROM admin_account") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert not auth._hasher.check_needs_rehash(row[0])


class TestSessions:
    async def test_create_and_validate(self, conn: aiosqlite.Connection) -> None:
        sid = await create_session(conn)
        assert await is_session_valid(conn, sid)
        assert not await is_session_valid(conn, sid + "x")

    async def test_only_hash_stored(self, conn: aiosqlite.Connection) -> None:
        sid = await create_session(conn)
        async with conn.execute("SELECT id_hash FROM admin_sessions") as cur:
            stored = [r[0] for r in await cur.fetchall()]
        assert sid not in stored
        assert len(stored) == 1

    async def test_delete_session(self, conn: aiosqlite.Connection) -> None:
        sid = await create_session(conn)
        await delete_session(conn, sid)
        assert not await is_session_valid(conn, sid)

    async def test_expired_session_invalid_and_purged(
        self, conn: aiosqlite.Connection
    ) -> None:
        sid = await create_session(conn)
        await conn.execute(
            "UPDATE admin_sessions SET expires_at = '2000-01-01T00:00:00+00:00'"
        )
        assert not await is_session_valid(conn, sid)
        await create_session(conn)
        async with conn.execute("SELECT COUNT(*) FROM admin_sessions") as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 1


class TestAdminTokens:
    async def test_create_and_verify(self, conn: aiosqlite.Connection) -> None:
        created = await create_admin_token(conn, "laptop")
        assert created.token.startswith(TOKEN_PREFIX)
        assert await verify_admin_token(conn, created.token) == created.id

    async def test_only_hash_stored(self, conn: aiosqlite.Connection) -> None:
        created = await create_admin_token(conn, "laptop")
        secret = created.token.split("_", 2)[2]
        async with conn.execute("SELECT id, token_hash FROM admin_tokens") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert secret not in row[1]
        assert created.token not in (row[0], row[1])

    async def test_verify_records_last_used(self, conn: aiosqlite.Connection) -> None:
        created = await create_admin_token(conn, "laptop")
        assert (await list_admin_tokens(conn))[0].last_used_at is None
        await verify_admin_token(conn, created.token)
        assert (await list_admin_tokens(conn))[0].last_used_at is not None

    @pytest.mark.parametrize(
        "mangle",
        [
            lambda t: t + "x",  # wrong secret
            lambda t: t[:-1],  # truncated secret
            lambda t: t.removeprefix(TOKEN_PREFIX),  # missing prefix
            lambda t: t.split("_", 2)[0] + "_" + t.split("_", 2)[1],  # no secret
            lambda t: TOKEN_PREFIX + "nothex_" + t.split("_", 2)[2],  # bad id
            lambda t: TOKEN_PREFIX + uuid.uuid4().hex + "_" + t.split("_", 2)[2],
            lambda t: "",
        ],
    )
    async def test_rejects_bad_tokens(
        self, conn: aiosqlite.Connection, mangle: Callable[[str], str]
    ) -> None:
        created = await create_admin_token(conn, "laptop")
        assert await verify_admin_token(conn, mangle(created.token)) is None

    async def test_list_has_no_secrets(self, conn: aiosqlite.Connection) -> None:
        a = await create_admin_token(conn, "a")
        b = await create_admin_token(conn, "b")
        tokens = await list_admin_tokens(conn)
        assert [t.id for t in tokens] == [a.id, b.id]
        assert all("token" not in t.model_dump() for t in tokens)

    async def test_delete(self, conn: aiosqlite.Connection) -> None:
        created = await create_admin_token(conn, "laptop")
        assert await delete_admin_token(conn, created.id)
        assert await verify_admin_token(conn, created.token) is None
        assert not await delete_admin_token(conn, created.id)


class TestSecretKey:
    def test_created_with_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "secret.key"
        key = load_or_create_secret_key(path)
        assert len(key) >= 32
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_reused_on_next_run(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.key"
        assert load_or_create_secret_key(path) == load_or_create_secret_key(path)

    def test_unique_per_install(self, tmp_path: Path) -> None:
        a = load_or_create_secret_key(tmp_path / "a.key")
        b = load_or_create_secret_key(tmp_path / "b.key")
        assert a != b

    def test_loose_permissions_tightened(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "secret.key"
        key = load_or_create_secret_key(path)
        path.chmod(0o644)
        assert load_or_create_secret_key(path) == key
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "tightening" in caplog.text

    def test_short_key_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "secret.key"
        path.write_text("short")
        path.chmod(0o600)
        with pytest.raises(RuntimeError, match="shorter than"):
            load_or_create_secret_key(path)


class TestLoginThrottle:
    def test_begin_attempt_counts_before_the_check(self) -> None:
        throttle = LoginThrottle(max_failures=2, window_seconds=60)
        assert throttle.begin_attempt("1.2.3.4")
        assert throttle.begin_attempt("1.2.3.4")
        assert not throttle.begin_attempt("1.2.3.4")
        throttle.reset("1.2.3.4")
        assert throttle.begin_attempt("1.2.3.4")

    def test_blocks_after_max_failures(self) -> None:
        throttle = LoginThrottle(max_failures=3, window_seconds=60)
        for _ in range(2):
            throttle.record_failure("1.2.3.4")
        assert not throttle.is_blocked("1.2.3.4")
        throttle.record_failure("1.2.3.4")
        assert throttle.is_blocked("1.2.3.4")
        assert not throttle.is_blocked("5.6.7.8")

    def test_reset(self) -> None:
        throttle = LoginThrottle(max_failures=1, window_seconds=60)
        throttle.record_failure("1.2.3.4")
        throttle.reset("1.2.3.4")
        assert not throttle.is_blocked("1.2.3.4")

    def test_failures_age_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1000.0]
        monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
        throttle = LoginThrottle(max_failures=1, window_seconds=60)
        throttle.record_failure("1.2.3.4")
        assert throttle.is_blocked("1.2.3.4")
        now[0] += 61
        assert not throttle.is_blocked("1.2.3.4")
