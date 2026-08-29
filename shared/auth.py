"""AsyncClient login with persistent session cache (data/session.json).

Credentials come from explicit args or the environment:
    BSKY_HANDLE / BSKY_APP_PASSWORD   (app password, not the account password)

The session string from a successful login is cached on disk (data/session.json,
see shared.paths) and reused on the next run; if it expires, we fall back to a
fresh login and refresh the cache.
"""

from __future__ import annotations

import json
import os

from atproto import AsyncClient
from atproto.exceptions import AtProtocolError

from shared.paths import data_root

SESSION_PATH = os.path.join(data_root(), "session.json")


class AuthError(Exception):
    """Raised when no credentials are available or login/session fails."""


def load_session(session_path: str = SESSION_PATH) -> str | None:
    if not os.path.exists(session_path):
        return None
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            return json.load(f).get("session")
    except (OSError, json.JSONDecodeError):
        return None


def save_session(session_string: str, session_path: str = SESSION_PATH) -> None:
    os.makedirs(os.path.dirname(session_path), exist_ok=True)
    with open(session_path, "w", encoding="utf-8") as f:
        json.dump({"session": session_string}, f, ensure_ascii=False, indent=2)


async def login_client(
    handle: str | None = None,
    password: str | None = None,
    session_path: str | None = None,
) -> AsyncClient:
    """Return a logged-in AsyncClient, reusing the cached session when valid.

    Tries, in order: cached session -> fresh login (args or env). The session
    cache is refreshed after a fresh login.
    """
    session_path = session_path or SESSION_PATH
    client = AsyncClient()

    cached = load_session(session_path)
    if cached:
        try:
            await client.login(session_string=cached)
            return client
        except AtProtocolError:
            pass  # expired/revoked -> fall through to a fresh login

    handle = handle or os.environ.get("BSKY_HANDLE")
    password = password or os.environ.get("BSKY_APP_PASSWORD")
    if not handle or not password:
        raise AuthError(
            "no credentials: pass --handle/--password or set BSKY_HANDLE / BSKY_APP_PASSWORD "
            "(use an app password, not the account password)"
        )

    try:
        await client.login(handle, password)
    except AtProtocolError as e:
        raise AuthError(f"login failed: {e}") from e

    save_session(client.export_session_string(), session_path)
    return client
