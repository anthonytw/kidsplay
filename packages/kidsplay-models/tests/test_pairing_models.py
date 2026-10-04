"""Tests for the shared pairing contract."""

import pytest
from pydantic import ValidationError

from kidsplay_models import (
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_MIN_SECRET_LENGTH,
    PairingCreate,
    PairingPoll,
    PairingPollResult,
    format_pairing_code,
    normalize_pairing_code,
)


class TestCode:
    def test_alphabet_has_no_lookalikes(self) -> None:
        assert not set("01OIL") & set(PAIRING_CODE_ALPHABET)
        assert len(set(PAIRING_CODE_ALPHABET)) == len(PAIRING_CODE_ALPHABET) == 31

    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            ("ABCD-2345", "ABCD2345"),
            ("abcd 2345", "ABCD2345"),
            ("  ab-cd-23-45 ", "ABCD2345"),
        ],
    )
    def test_normalize(self, raw: str, code: str) -> None:
        assert normalize_pairing_code(raw) == code

    @pytest.mark.parametrize(
        "raw", ["", "ABCD", "ABCD-234", "ABCD-23456", "ABCD-0OIL", "ABCD-234\n5!"]
    )
    def test_normalize_rejects(self, raw: str) -> None:
        assert normalize_pairing_code(raw) is None

    def test_format(self) -> None:
        assert format_pairing_code("ABCD2345") == "ABCD-2345"
        assert PAIRING_CODE_LENGTH == 8


class TestModels:
    def test_create_defaults(self) -> None:
        body = PairingCreate(code="ABCD-2345", binding_secret="s" * 32)
        assert (body.display_width, body.display_height) == (640, 480)
        assert body.device_name == ""

    def test_short_secret_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PairingCreate(
                code="ABCD-2345", binding_secret="s" * (PAIRING_MIN_SECRET_LENGTH - 1)
            )

    def test_secrets_are_not_in_repr(self) -> None:
        secret = "s" * 40
        assert secret not in repr(PairingCreate(code="ABCD2345", binding_secret=secret))
        assert secret not in repr(PairingPoll(code="ABCD2345", binding_secret=secret))
        result = PairingPollResult(status="approved", api_key="k" * 32)
        assert "k" * 32 not in repr(result)

    def test_pending_result_has_no_credentials(self) -> None:
        result = PairingPollResult(status="pending")
        assert result.device_id is None
        assert result.api_key is None
