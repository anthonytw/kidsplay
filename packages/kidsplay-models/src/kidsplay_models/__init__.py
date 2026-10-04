"""KidsPlay shared data models.

All packages in the kidsplay monorepo import their data contracts from here.
This is the single source of truth for all data types in the system.

Usage:
    from kidsplay_models import MediaItem, Device, Profile, SyncManifest
    from kidsplay_models.media import MediaType
    from kidsplay_models.processing import ProcessingStatus, ThumbnailSize
"""

from .auth import (
    AdminLoginRequest,
    AdminPasswordChange,
    AdminToken,
    AdminTokenCreate,
    AdminTokenCreated,
    AuthStatus,
)
from .device import (
    Device,
    DeviceCreate,
    Profile,
    ProfileCreate,
    ProfileMediaAssignment,
)
from .i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGE_NAMES,
    LEGACY_DEVICE_LANGUAGE,
    SUPPORTED_LANGUAGES,
    language_from_environment,
    load_translations,
    negotiate_accept_language,
    normalize_language,
)
from .media import (
    MediaItem,
    MediaType,
)
from .pairing import (
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_MIN_SECRET_LENGTH,
    PAIRING_POLL_INTERVAL_SECONDS,
    SERVER_ID_HEADER,
    PairingCreate,
    PairingCreated,
    PairingPoll,
    PairingPollResult,
    format_pairing_code,
    normalize_pairing_code,
    short_server_id,
)
from .processing import (
    IngestBatchResult,
    IngestRequest,
    IngestResult,
    NormalizeRequest,
    NormalizeStatus,
    ProcessedFile,
    ProcessingStatus,
    ThumbnailSize,
)
from .queue import (
    QueueItem,
    QueueRequest,
    QueueStatus,
)
from .settings import (
    PROFILE_SETTINGS_VERSION,
    BedtimeMode,
    BedtimeWindow,
    ProfileSettings,
    Weekday,
)
from .sync import (
    SyncFileEntry,
    SyncManifest,
    SyncMediaEntry,
)
from .themes import (
    BUILTIN_THEMES,
    BUILTIN_THEMES_BY_ID,
    DEFAULT_THEME_ID,
    THEME_ID_PATTERN,
    ThemeAsset,
    ThemeAssetRole,
    ThemeColors,
    ThemeDefinition,
    contrast_ratio,
)

__all__ = [
    # On-device pairing
    "PAIRING_CODE_ALPHABET",
    "PAIRING_CODE_LENGTH",
    "PAIRING_MIN_SECRET_LENGTH",
    "PAIRING_POLL_INTERVAL_SECONDS",
    "SERVER_ID_HEADER",
    "PairingCreate",
    "PairingCreated",
    "PairingPoll",
    "PairingPollResult",
    "format_pairing_code",
    "normalize_pairing_code",
    "short_server_id",
    # Localization
    "DEFAULT_LANGUAGE",
    "LANGUAGE_NAMES",
    "LEGACY_DEVICE_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "language_from_environment",
    "load_translations",
    "negotiate_accept_language",
    "normalize_language",
    # Admin auth
    "AdminLoginRequest",
    "AdminPasswordChange",
    "AdminToken",
    "AdminTokenCreate",
    "AdminTokenCreated",
    "AuthStatus",
    # Media
    "MediaItem",
    "MediaType",
    # Devices & profiles
    "Device",
    "DeviceCreate",
    "Profile",
    "ProfileCreate",
    "ProfileMediaAssignment",
    # Processing
    "IngestBatchResult",
    "IngestRequest",
    "IngestResult",
    "NormalizeRequest",
    "NormalizeStatus",
    "ProcessedFile",
    "ProcessingStatus",
    "ThumbnailSize",
    # Queue
    "QueueStatus",
    "QueueItem",
    "QueueRequest",
    # Profile settings
    "PROFILE_SETTINGS_VERSION",
    "BedtimeMode",
    "BedtimeWindow",
    "ProfileSettings",
    "Weekday",
    # Sync
    "SyncFileEntry",
    "SyncManifest",
    "SyncMediaEntry",
    # Themes
    "BUILTIN_THEMES",
    "BUILTIN_THEMES_BY_ID",
    "DEFAULT_THEME_ID",
    "THEME_ID_PATTERN",
    "ThemeAsset",
    "ThemeAssetRole",
    "ThemeColors",
    "ThemeDefinition",
    "contrast_ratio",
]
