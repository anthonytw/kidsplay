"""Tests for the admin authentication models."""

import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from kidsplay_models import (
    AdminLoginRequest,
    AdminToken,
    AdminTokenCreate,
    AdminTokenCreated,
    AuthStatus,
)
from kidsplay_models.auth import MAX_PASSWORD_LENGTH


class TestAdminLoginRequest:
    def test_default_token_name(self) -> None:
        assert AdminLoginRequest(password="pw").token_name == "kidsplay-cli"

    def test_empty_password_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdminLoginRequest(password="")

    def test_overlong_password_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdminLoginRequest(password="x" * (MAX_PASSWORD_LENGTH + 1))

    def test_empty_token_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdminLoginRequest(password="pw", token_name="")


class TestAdminTokenCreate:
    def test_valid(self) -> None:
        assert AdminTokenCreate(name="laptop").name == "laptop"

    def test_overlong_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AdminTokenCreate(name="x" * 101)


class TestAdminToken:
    def test_listing_has_no_secret(self) -> None:
        tok = AdminToken(id=uuid.uuid4(), name="cli", created_at=datetime.now())
        assert "token" not in tok.model_dump()
        assert tok.last_used_at is None

    def test_created_includes_secret(self) -> None:
        tok = AdminTokenCreated(
            id=uuid.uuid4(), name="cli", created_at=datetime.now(), token="kpa_x"
        )
        assert tok.model_dump()["token"] == "kpa_x"


class TestAuthStatus:
    def test_round_trip(self) -> None:
        status = AuthStatus(auth_enabled=True, method="token")
        assert AuthStatus.model_validate_json(status.model_dump_json()) == status

    def test_unknown_method_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AuthStatus.model_validate({"auth_enabled": True, "method": "password"})
