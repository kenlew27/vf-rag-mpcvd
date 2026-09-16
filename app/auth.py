"""Small proxy-auth boundary for the VM beta deployment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from fastapi import HTTPException, Request, status


PROXY_USER_HEADER = "x-app-authenticated-user"
REQUIRE_PROXY_AUTH_ENV = "APP_REQUIRE_PROXY_AUTH"
TRUSTED_PROXY_IPS_ENV = "APP_TRUSTED_PROXY_IPS"
ALLOWED_PROXY_USERS_ENV = "APP_ALLOWED_PROXY_USERS"

_USER_RE = re.compile(r"^[A-Za-z0-9._%+\-@]{1,254}$")


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str


def proxy_auth_enabled() -> bool:
    return _truthy(os.environ.get(REQUIRE_PROXY_AUTH_ENV))


def authenticate_proxy_request(request: Request) -> None:
    """Attach the nginx-authenticated user to request.state when enabled."""
    if not proxy_auth_enabled():
        return

    client_host = request.client.host if request.client else ""
    if client_host not in _trusted_proxy_ips():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Request did not come through the trusted local proxy.")

    user_id = request.headers.get(PROXY_USER_HEADER, "").strip()
    if not user_id:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication is required.",
            headers={"WWW-Authenticate": 'Basic realm="app beta"'},
        )
    if not _USER_RE.fullmatch(user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Authenticated user id is invalid.")

    allowed_users = _allowed_proxy_users()
    if allowed_users and user_id.lower() not in allowed_users:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Authenticated user is not allowed.")

    request.state.authenticated_user = AuthenticatedUser(user_id=user_id)


def current_user(request: Request) -> AuthenticatedUser | None:
    value = getattr(request.state, "authenticated_user", None)
    return value if isinstance(value, AuthenticatedUser) else None


def owner_session_id_for_request(request: Request, fallback: str) -> str:
    user = current_user(request)
    return user.user_id if user is not None else fallback


def _trusted_proxy_ips() -> set[str]:
    value = os.environ.get(TRUSTED_PROXY_IPS_ENV, "127.0.0.1,::1")
    return {item.strip() for item in value.split(",") if item.strip()}


def _allowed_proxy_users() -> set[str]:
    value = os.environ.get(ALLOWED_PROXY_USERS_ENV, "")
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
