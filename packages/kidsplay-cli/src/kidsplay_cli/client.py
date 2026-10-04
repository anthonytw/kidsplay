"""HTTP client for the KidsPlay server API.

Wraps httpx with async methods that mirror each REST endpoint.  All methods
raise ``KidsPlayError`` on non-2xx responses so callers never need to inspect
status codes directly.

Management endpoints need an admin API token (``kidsplay auth login``)::

    async with KidsPlayClient("http://kidsplay.local:8000", token) as client:
        profiles = await client.list_profiles()
        for p in profiles:
            print(p["name"])
"""

import uuid
from typing import Any

import httpx


class KidsPlayError(Exception):
    """Raised when the server returns a non-2xx response.

    Attributes:
        status_code: HTTP status code from the response.
        detail: Human-readable error message from the server.
        error_code: Machine-readable code from the server (may be empty).
    """

    def __init__(self, status_code: int, detail: str, error_code: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code
        super().__init__(f"HTTP {status_code}: {detail}")


class KidsPlayClient:
    """Async HTTP client for the KidsPlay server REST API.

    All methods correspond directly to an API endpoint.  Responses are
    returned as plain dicts/lists (parsed JSON) — no model validation — so
    the CLI layer can use them without importing kidsplay-models.

    Args:
        base_url: Server base URL, e.g. ``http://localhost:8000``.
            Trailing slash is stripped automatically.
        token: Admin API token, sent as ``Authorization: Bearer``. Only
            ``login`` works without one (unless the server disables auth).
    """

    def __init__(
        self, base_url: str = "http://localhost:8000", token: str | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "KidsPlayClient":
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        self._client = httpx.AsyncClient(
            base_url=self._base_url, headers=headers, timeout=30.0
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "KidsPlayClient must be used as an async context manager"
            )
        return self._client

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Raise ``KidsPlayError`` for non-2xx responses.

        Parses the documented ``{detail, error_code}`` error format when
        the response body is JSON; falls back to the raw text otherwise.
        """
        if response.is_success:
            return
        detail = str(response.status_code)
        error_code = ""
        try:
            body = response.json()
            detail = body.get("detail", detail)
            error_code = body.get("error_code", "")
        except Exception:
            detail = response.text or detail
        raise KidsPlayError(response.status_code, detail, error_code)

    # Any: the return is decoded JSON whose shape each caller annotates, and
    # query params are forwarded to httpx as-is.
    async def _get(self, path: str, **params: Any) -> Any:  # noqa: ANN401
        filtered = {k: v for k, v in params.items() if v is not None}
        r = await self._http.get(f"/api/v1{path}", params=filtered)
        self._raise_for_status(r)
        return r.json()

    async def _post(self, path: str, body: dict) -> Any:  # noqa: ANN401
        r = await self._http.post(f"/api/v1{path}", json=body)
        self._raise_for_status(r)
        return r.json()

    async def _put(self, path: str, body: dict) -> Any:  # noqa: ANN401
        r = await self._http.put(f"/api/v1{path}", json=body)
        self._raise_for_status(r)
        return r.json()

    async def _delete(self, path: str) -> None:
        r = await self._http.delete(f"/api/v1{path}")
        self._raise_for_status(r)

    # ------------------------------------------------------------------
    # Admin auth
    # ------------------------------------------------------------------

    async def login(self, password: str, token_name: str) -> dict:
        """Exchange the admin password for a new admin API token.

        Args:
            password: Admin password.
            token_name: Label for the token in the server's token list.

        Returns:
            ``AdminTokenCreated`` dict with ``id``, ``name``, ``created_at``
            and the secret ``token``.

        Raises:
            KidsPlayError: 401 wrong password, 409 setup not done, 429
                throttled.
        """
        result: dict = await self._post(
            "/auth/login", {"password": password, "token_name": token_name}
        )
        return result

    async def auth_status(self) -> dict:
        """Report how this client is authenticated.

        Returns:
            ``AuthStatus`` dict with ``auth_enabled`` and ``method``.

        Raises:
            KidsPlayError: 401 if the token is missing or invalid.
        """
        result: dict = await self._get("/auth/status")
        return result

    async def revoke_token(self, token_id: str | uuid.UUID) -> None:
        """Revoke an admin API token.

        Args:
            token_id: Id of the token to revoke.
        """
        await self._delete(f"/auth/tokens/{token_id}")

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    async def list_profiles(self) -> list[dict]:
        """List all profiles.

        Returns:
            List of profile dicts with keys ``id``, ``name``, ``created_at``.
        """
        result: list[dict] = await self._get("/profiles")
        return result

    async def create_profile(self, name: str) -> dict:
        """Create a new profile.

        Args:
            name: Child's display name.

        Returns:
            Created profile dict.
        """
        result: dict = await self._post("/profiles", {"name": name})
        return result

    async def delete_profile(self, profile_id: str | uuid.UUID) -> None:
        """Delete a profile.

        Args:
            profile_id: UUID of the profile to delete.

        Raises:
            KidsPlayError: 409 if devices are still linked to this profile.
        """
        await self._delete(f"/profiles/{profile_id}")

    async def get_profile_settings(self, profile_id: str | uuid.UUID) -> dict:
        """Fetch a profile's settings.

        Args:
            profile_id: UUID of the profile.

        Returns:
            ``ProfileSettings`` as a dict.

        Raises:
            KidsPlayError: 404 if the profile does not exist.
        """
        result: dict = await self._get(f"/profiles/{profile_id}/settings")
        return result

    async def put_profile_settings(
        self, profile_id: str | uuid.UUID, settings: dict
    ) -> dict:
        """Replace a profile's settings.

        Args:
            profile_id: UUID of the profile.
            settings: Complete ``ProfileSettings`` as a dict.

        Returns:
            The stored settings.

        Raises:
            KidsPlayError: 404 if the profile does not exist, 422 if the
                settings are invalid.
        """
        result: dict = await self._put(f"/profiles/{profile_id}/settings", settings)
        return result

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    async def list_devices(self) -> list[dict]:
        """List all registered devices.

        Returns:
            List of device dicts.
        """
        result: list[dict] = await self._get("/devices")
        return result

    async def create_device(
        self,
        name: str,
        profile_id: str | uuid.UUID,
        display_width: int = 640,
        display_height: int = 480,
    ) -> dict:
        """Register a new device.

        Args:
            name: Human-readable device name.
            profile_id: UUID of the child's profile.
            display_width: Screen width in pixels (default 640).
            display_height: Screen height in pixels (default 480).

        Returns:
            Created device dict including generated ``api_key``.
        """
        result: dict = await self._post(
            "/devices",
            {
                "name": name,
                "profile_id": str(profile_id),
                "display_width": display_width,
                "display_height": display_height,
            },
        )
        return result

    async def delete_device(self, device_id: str | uuid.UUID) -> None:
        """Unregister a device.

        Args:
            device_id: UUID of the device to remove.
        """
        await self._delete(f"/devices/{device_id}")

    # ------------------------------------------------------------------
    # Media
    # ------------------------------------------------------------------

    async def ingest(
        self,
        source_path: str,
        media_type: str,
        playlist_title: str,
        profile_ids: list[str] | None = None,
    ) -> dict:
        """Ingest media from a server-side filesystem path.

        Args:
            source_path: Absolute path to file or directory on the server.
            media_type: ``'music'``, ``'audiobook'``, or ``'photo'``.
            playlist_title: Playlist/group name for all ingested items.
            profile_ids: Optional list of profile UUIDs to assign after ingest.

        Returns:
            ``IngestBatchResult`` dict with ``total_files``, ``successful``,
            ``failed``, ``skipped``, and ``results``.
        """
        result: dict = await self._post(
            "/media/ingest",
            {
                "source_path": source_path,
                "media_type": media_type,
                "playlist_title": playlist_title,
                "profile_ids": profile_ids or [],
            },
        )
        return result

    # ------------------------------------------------------------------
    # Importers
    # ------------------------------------------------------------------

    async def list_importers(self) -> list[dict]:
        """List the importers installed on the server.

        Returns:
            List of dicts with keys ``name``, ``label``, ``requires_queue``
            and ``supports_preview``, in the order the server tries them.
        """
        result: list[dict] = await self._get("/importers")
        return result

    async def list_media(
        self,
        media_type: str | None = None,
        profile_id: str | uuid.UUID | None = None,
        playlist_title: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List media items with optional filtering.

        Args:
            media_type: Filter by type (``'music'``, ``'audiobook'``, ``'photo'``).
            profile_id: Only media assigned to this profile.
            playlist_title: Exact-match filter on playlist title.
            q: Substring search across title, artist, playlist_title.
            limit: Maximum results (default 100).
            offset: Pagination offset.

        Returns:
            List of media item dicts.
        """
        result: list[dict] = await self._get(
            "/media",
            media_type=media_type,
            profile_id=str(profile_id) if profile_id else None,
            playlist_title=playlist_title,
            q=q,
            limit=limit,
            offset=offset,
        )
        return result

    async def get_media(self, media_id: str | uuid.UUID) -> dict:
        """Fetch a single media item by ID.

        Args:
            media_id: UUID of the media item.

        Returns:
            Media item dict.

        Raises:
            KidsPlayError: 404 if not found.
        """
        result: dict = await self._get(f"/media/{media_id}")
        return result

    async def start_normalize(
        self, *, all_media: bool = False, media_ids: list[str] | None = None
    ) -> dict:
        """Start the server's background loudness backfill.

        Args:
            all_media: Normalize every music and audiobook item.
            media_ids: Normalize only these items (when ``all_media`` is False).

        Returns:
            Backfill status dict.

        Raises:
            KidsPlayError: 409 ``BACKFILL_RUNNING`` if one is in progress.
        """
        result: dict = await self._post(
            "/media/normalize",
            {"all": all_media, "media_ids": [str(m) for m in media_ids or []]},
        )
        return result

    async def normalize_status(self) -> dict:
        """Fetch the progress of the current or last loudness backfill.

        Returns:
            Backfill status dict.
        """
        result: dict = await self._get("/media/normalize")
        return result

    async def delete_media(self, media_id: str | uuid.UUID) -> None:
        """Delete a media item and all its processed files.

        Args:
            media_id: UUID of the media item.
        """
        await self._delete(f"/media/{media_id}")

    async def assign_media(
        self,
        media_id: str | uuid.UUID,
        profile_ids: list[str | uuid.UUID],
    ) -> list[dict]:
        """Assign a media item to one or more profiles.

        Args:
            media_id: UUID of the media item.
            profile_ids: List of profile UUIDs.

        Returns:
            List of assignment dicts.
        """
        result: list[dict] = await self._post(
            f"/media/{media_id}/assign",
            {"profile_ids": [str(p) for p in profile_ids]},
        )
        return result

    async def unassign_media(
        self,
        media_id: str | uuid.UUID,
        profile_id: str | uuid.UUID,
    ) -> None:
        """Remove a media-to-profile assignment.

        Args:
            media_id: UUID of the media item.
            profile_id: UUID of the profile.
        """
        await self._delete(f"/media/{media_id}/assign/{profile_id}")
