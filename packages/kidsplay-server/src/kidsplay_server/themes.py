"""Looking up themes: the built-in ones (code) and the custom ones (database)."""

import aiosqlite

from kidsplay_models import BUILTIN_THEMES, BUILTIN_THEMES_BY_ID, ThemeDefinition
from kidsplay_server.database import get_custom_theme, list_custom_themes
from kidsplay_server.i18n import N_, gettext_now

# The built-in names are message ids, translated where they are shown. Listed
# here so ``just i18n-extract`` finds them; keep in step with BUILTIN_THEMES.
BUILTIN_THEME_LABELS: tuple[str, ...] = (
    N_("Blue"),
    N_("Purple"),
    N_("Green"),
    N_("Red"),
    N_("Orange"),
    N_("Cyan"),
    N_("High contrast"),
    N_("Night"),
)


def theme_label(theme: ThemeDefinition) -> str:
    """The name to show for a theme in the web UI's language.

    Args:
        theme: Any theme.

    Returns:
        The translated name of a built-in theme, or a custom theme's name
        as its owner wrote it.
    """
    return gettext_now(theme.name) if theme.builtin else theme.name


async def list_themes(conn: aiosqlite.Connection) -> list[ThemeDefinition]:
    """All themes a profile can choose: built-ins first, then custom ones.

    Args:
        conn: Open, configured connection.

    Returns:
        The built-in themes followed by the custom themes.
    """
    return [*BUILTIN_THEMES, *await list_custom_themes(conn)]


async def resolve_theme(
    conn: aiosqlite.Connection, theme_id: str
) -> ThemeDefinition | None:
    """Find a theme by id, built-in or custom.

    Args:
        conn: Open, configured connection.
        theme_id: The id from ``ProfileSettings.theme``.

    Returns:
        The theme, or None if no theme has that id (say, a deleted one).
    """
    builtin = BUILTIN_THEMES_BY_ID.get(theme_id)
    if builtin is not None:
        return builtin
    return await get_custom_theme(conn, theme_id)
