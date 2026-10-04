"""Local storage of admin API tokens for the CLI.

``kidsplay auth login`` stores one token per server in
``~/.config/kidsplay/credentials.json`` (``$XDG_CONFIG_HOME`` is honoured).
The file is written with mode 0600 inside a 0700 directory, since the tokens
grant full admin access::

    {"servers": {"http://kidsplay.local:8000": {"token": "kpa_...",
                                                "token_id": "..."}}}
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .i18n import _


@dataclass(frozen=True)
class StoredToken:
    """An admin API token saved for one server.

    Attributes:
        token: The secret token string.
        token_id: Server-side token id, used to revoke it on logout.
    """

    token: str
    token_id: str


def credentials_path() -> Path:
    """Return the path of the credentials file.

    Returns:
        ``$XDG_CONFIG_HOME/kidsplay/credentials.json``, defaulting
        ``XDG_CONFIG_HOME`` to ``~/.config``.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "kidsplay" / "credentials.json"


def _server_key(server: str) -> str:
    return server.rstrip("/")


def _read_all() -> dict[str, dict[str, str]]:
    path = credentials_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            _("Cannot read credentials file {path}: {exc}").format(path=path, exc=exc)
        ) from exc
    servers = data.get("servers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def _tighten_directory(directory: Path) -> None:
    """Make ``directory`` private (0700) if it is more open than that.

    ``mkdir(mode=...)`` only applies to a directory it creates, so one made
    earlier (by another tool, or by hand with the default umask) keeps its
    looser mode. Failing to change it is not fatal: the file itself is 0600.
    """
    try:
        if directory.stat().st_mode & 0o077:
            directory.chmod(0o700)
    except OSError:
        pass  # not ours to change (another owner, read-only); the file is 0600


def _write_all(servers: dict[str, dict[str, str]]) -> None:
    path = credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _tighten_directory(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    # Create the temp file 0600 from the start, then atomically replace.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"servers": servers}, f, indent=2)
    os.replace(tmp, path)


def load_token(server: str) -> StoredToken | None:
    """Return the stored token for ``server``, if any.

    Args:
        server: Server base URL (a trailing slash is ignored).

    Returns:
        The stored token, or None if not logged in to that server.

    Raises:
        RuntimeError: If the credentials file exists but cannot be parsed.
    """
    entry = _read_all().get(_server_key(server))
    if not isinstance(entry, dict):
        return None
    token = entry.get("token")
    if not isinstance(token, str) or not token:
        return None
    return StoredToken(token=token, token_id=str(entry.get("token_id", "")))


def save_token(server: str, token: StoredToken) -> Path:
    """Store the token for ``server``, replacing any previous one.

    Args:
        server: Server base URL.
        token: Token to store.

    Returns:
        Path of the credentials file.
    """
    servers = _read_all()
    servers[_server_key(server)] = {"token": token.token, "token_id": token.token_id}
    _write_all(servers)
    return credentials_path()


def delete_token(server: str) -> bool:
    """Remove the stored token for ``server``.

    Args:
        server: Server base URL.

    Returns:
        True if a token was removed.
    """
    servers = _read_all()
    if servers.pop(_server_key(server), None) is None:
        return False
    _write_all(servers)
    return True
