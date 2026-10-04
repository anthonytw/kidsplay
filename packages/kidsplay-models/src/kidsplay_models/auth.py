"""Admin authentication models.

The server has a single admin account. Browsers authenticate with a session
cookie; the CLI and scripts authenticate with admin API tokens. These models
are the request/response contracts for the ``/api/v1/auth`` endpoints.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Upper bound on password input so a huge body cannot make argon2 hash
# megabytes of data per request.
MAX_PASSWORD_LENGTH = 1024


class AdminLoginRequest(BaseModel):
    """Exchange the admin password for a new admin API token."""

    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    token_name: str = Field(
        default="kidsplay-cli",
        min_length=1,
        max_length=100,
        description="Label for the new token, shown in the token list.",
    )


class AdminPasswordChange(BaseModel):
    """Change the admin password (``PUT /api/v1/auth/password``).

    The current password is required even for a logged-in admin, so a stolen
    session or token cannot lock the owner out.
    """

    current_password: str = Field(
        min_length=1, max_length=MAX_PASSWORD_LENGTH, repr=False
    )
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH, repr=False)
    revoke_tokens: bool = False
    """Also revoke every admin API token (default: tokens keep working)."""


class AdminTokenCreate(BaseModel):
    """Request model for creating an admin API token as a logged-in admin."""

    name: str = Field(min_length=1, max_length=100)


class AdminToken(BaseModel):
    """An admin API token as listed by the server (never includes the secret)."""

    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None = None


class AdminTokenCreated(AdminToken):
    """A newly created admin API token.

    ``token`` is the only time the secret is ever returned; the server keeps
    only a hash of it.
    """

    token: str = Field(description="Send as 'Authorization: Bearer <token>'.")


class AuthStatus(BaseModel):
    """How the current request was authenticated."""

    auth_enabled: bool
    method: Literal["session", "token", "disabled"]
