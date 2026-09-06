from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

from rebase.config import DEFAULT_PLATFORM, active_platform, platform_auth_path


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthSession:
    access_token: str
    refresh_token: str | None
    expires_at: int | None
    token_type: str
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    email: str | None = None
    user_id: str | None = None
    provider: str | None = None

    @property
    def expires_at_datetime(self) -> datetime | None:
        if self.expires_at is None:
            return None
        return datetime.fromtimestamp(self.expires_at, UTC)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= int(datetime.now(UTC).timestamp())

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "supabase_url": self.supabase_url,
            "supabase_anon_key": self.supabase_anon_key,
            "email": self.email,
            "user_id": self.user_id,
            "provider": self.provider,
        }


def auth_file_path() -> Path:
    override = os.getenv("REBASE_AUTH_FILE") or os.getenv("REBASE_WORKFLOWS_AUTH_FILE")
    if override:
        return Path(override).expanduser()
    config_home = Path(os.getenv("XDG_CONFIG_HOME", "~/.config")).expanduser()
    # A second deployment signs in through its own identity provider, so its session
    # is its own file: signing in there must not replace the session for the first.
    platform = active_platform()
    if platform != DEFAULT_PLATFORM:
        return platform_auth_path(platform, config_home)
    return config_home / "rebase" / "auth.json"


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    _header, separator, remainder = token.partition(".")
    if not separator:
        return {}
    payload, separator, _signature = remainder.partition(".")
    if not separator:
        return {}
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}")
        value = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _provider_from_payload(payload: dict[str, Any]) -> str | None:
    """Which social login issued this token, per Supabase's `app_metadata`.

    Worth keeping because the account someone is signed in as is otherwise invisible:
    an invite sent to a work address and a GitHub login that reports a private
    noreply address look identical until the provider is named.
    """
    app_metadata = payload.get("app_metadata")
    provider = app_metadata.get("provider") if isinstance(app_metadata, dict) else None
    return provider if isinstance(provider, str) and provider else None


def _supabase_url_from_issuer(issuer: str | None) -> str | None:
    if not issuer:
        return None
    suffix = "/auth/v1"
    return issuer[: -len(suffix)] if issuer.endswith(suffix) else None


def parse_callback_url(callback_url: str) -> AuthSession:
    parsed = urlsplit(callback_url.strip())
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update(parse_qsl(parsed.fragment, keep_blank_values=True))

    access_token = params.get("access_token")
    if not access_token:
        raise AuthError("callback URL does not contain an access_token")

    payload = _decode_jwt_payload(access_token)
    expires_at_raw = params.get("expires_at") or payload.get("exp")
    try:
        expires_at = int(expires_at_raw) if expires_at_raw is not None else None
    except (TypeError, ValueError):
        expires_at = None

    token_type = params.get("token_type") or "bearer"
    if token_type.lower() != "bearer":
        raise AuthError(f"unsupported token type: {token_type}")

    email = payload.get("email") if isinstance(payload.get("email"), str) else None
    user_id = payload.get("sub") if isinstance(payload.get("sub"), str) else None
    issuer = payload.get("iss") if isinstance(payload.get("iss"), str) else None

    return AuthSession(
        access_token=access_token,
        refresh_token=params.get("refresh_token") or None,
        expires_at=expires_at,
        token_type=token_type,
        supabase_url=_supabase_url_from_issuer(issuer),
        email=email,
        user_id=user_id,
        provider=_provider_from_payload(payload),
    )


def generate_pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def build_supabase_authorize_url(
    *,
    supabase_url: str,
    provider: str,
    redirect_to: str,
    code_challenge: str,
) -> str:
    query = urlencode(
        {
            "provider": provider,
            "redirect_to": redirect_to,
            "flow_type": "pkce",
            "code_challenge": code_challenge,
            "code_challenge_method": "s256",
        }
    )
    return f"{supabase_url.rstrip('/')}/auth/v1/authorize?{query}"


def fetch_link_identity_url(
    *,
    supabase_url: str,
    supabase_anon_key: str,
    access_token: str,
    provider: str,
    redirect_to: str,
    code_challenge: str,
) -> str:
    """Ask Supabase for the provider URL that links `provider` to the signed-in user.

    Unlike ``/authorize`` (anonymous sign-in, which creates or signs in a possibly
    different user), ``/user/identities/authorize`` is authenticated: the OAuth flow
    it starts attaches the new identity to the token's user. Requires manual
    linking to be enabled on the Supabase project.
    """
    response = requests.get(
        f"{supabase_url.rstrip('/')}/auth/v1/user/identities/authorize",
        params={
            "provider": provider,
            "redirect_to": redirect_to,
            "flow_type": "pkce",
            "code_challenge": code_challenge,
            "code_challenge_method": "s256",
            "skip_http_redirect": "true",
        },
        headers={
            "apikey": supabase_anon_key,
            "Authorization": f"Bearer {access_token}",
        },
        timeout=30,
    )
    if not response.ok:
        detail = response.text
        with suppress(ValueError):
            body = response.json()
            if isinstance(body, dict):
                if body.get("error_code") == "manual_linking_disabled":
                    raise AuthError(
                        "manual identity linking is disabled on the Supabase project — "
                        "enable it under Authentication settings and try again"
                    )
                detail = body.get("msg") or body.get("message") or body.get("error_description") or detail
        raise AuthError(f"could not start the identity link flow: {detail}")
    payload = response.json()
    url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(url, str) or not url:
        raise AuthError("Supabase did not return a link URL")
    return url


def fetch_user_identities(
    *,
    supabase_url: str,
    supabase_anon_key: str,
    access_token: str,
) -> list[dict[str, Any]]:
    """The identities (login methods) already attached to the token's user."""
    response = requests.get(
        f"{supabase_url.rstrip('/')}/auth/v1/user",
        headers={
            "apikey": supabase_anon_key,
            "Authorization": f"Bearer {access_token}",
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise AuthError(f"could not read the signed-in user: {response.text}") from exc
    payload = response.json()
    identities = payload.get("identities") if isinstance(payload, dict) else None
    if not isinstance(identities, list):
        return []
    return [identity for identity in identities if isinstance(identity, dict)]


def github_username_from_jwt(access_token: str) -> str | None:
    """The GitHub login carried by this token, if any.

    Mirrors the claim order the toolkit backend uses to fill
    ``profiles.github_username``, so "the token carries a username" here means
    the backend will see it too.
    """
    payload = _decode_jwt_payload(access_token)
    candidates: list[Any] = [payload.get(key) for key in ("user_name", "preferred_username", "nickname")]
    metadata = payload.get("user_metadata")
    if isinstance(metadata, dict):
        candidates.extend(metadata.get(key) for key in ("user_name", "preferred_username", "nickname", "login"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _session_from_token_payload(
    payload: dict[str, Any],
    *,
    supabase_url: str,
    supabase_anon_key: str | None,
) -> AuthSession:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthError("Supabase token response did not include access_token")
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, int):
        expires_in = payload.get("expires_in")
        expires_at = int(datetime.now(UTC).timestamp()) + int(expires_in) if isinstance(expires_in, int) else None
    token_type_value = payload.get("token_type")
    token_type = token_type_value if isinstance(token_type_value, str) else "bearer"
    jwt_payload = _decode_jwt_payload(access_token)
    email = jwt_payload.get("email") if isinstance(jwt_payload.get("email"), str) else None
    user_id = jwt_payload.get("sub") if isinstance(jwt_payload.get("sub"), str) else None
    return AuthSession(
        access_token=access_token,
        refresh_token=payload.get("refresh_token") if isinstance(payload.get("refresh_token"), str) else None,
        expires_at=expires_at,
        token_type=token_type,
        supabase_url=supabase_url.rstrip("/"),
        supabase_anon_key=supabase_anon_key,
        email=email,
        user_id=user_id,
        provider=_provider_from_payload(jwt_payload),
    )


def exchange_pkce_code(
    *,
    supabase_url: str,
    supabase_anon_key: str,
    auth_code: str,
    code_verifier: str,
) -> AuthSession:
    response = requests.post(
        f"{supabase_url.rstrip('/')}/auth/v1/token",
        params={"grant_type": "pkce"},
        headers={
            "apikey": supabase_anon_key,
            "Authorization": f"Bearer {supabase_anon_key}",
            "Content-Type": "application/json",
        },
        json={"auth_code": auth_code, "code_verifier": code_verifier},
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise AuthError(response.text) from exc
    payload = response.json()
    if not isinstance(payload, dict):
        raise AuthError("Supabase token response was not a JSON object")
    return _session_from_token_payload(payload, supabase_url=supabase_url, supabase_anon_key=supabase_anon_key)


def refresh_session(session: AuthSession) -> AuthSession:
    if not session.refresh_token:
        raise AuthError("stored Supabase session is expired and has no refresh token")
    if not session.supabase_url:
        raise AuthError("stored Supabase session is missing supabase_url")
    supabase_anon_key = session.supabase_anon_key or os.getenv("REBASE_SUPABASE_ANON_KEY")
    if not supabase_anon_key:
        raise AuthError("stored Supabase session is expired and no Supabase anon key is available")
    response = requests.post(
        f"{session.supabase_url.rstrip('/')}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        headers={
            "apikey": supabase_anon_key,
            "Authorization": f"Bearer {supabase_anon_key}",
            "Content-Type": "application/json",
        },
        json={"refresh_token": session.refresh_token},
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise AuthError(response.text) from exc
    payload = response.json()
    if not isinstance(payload, dict):
        raise AuthError("Supabase refresh response was not a JSON object")
    refreshed = _session_from_token_payload(
        payload,
        supabase_url=session.supabase_url,
        supabase_anon_key=supabase_anon_key,
    )
    save_session(refreshed)
    return refreshed


def save_session(session: AuthSession, *, path: Path | None = None) -> Path:
    destination = path or auth_file_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        destination.parent.chmod(0o700)
    temp_path = destination.with_suffix(f"{destination.suffix}.tmp")
    temp_path.write_text(json.dumps(session.to_json(), indent=2, sort_keys=True) + "\n")
    temp_path.chmod(0o600)
    temp_path.replace(destination)
    destination.chmod(0o600)
    return destination


def load_session(*, path: Path | None = None) -> AuthSession | None:
    source = path or auth_file_path()
    if not source.exists():
        return None
    try:
        data = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"could not read auth session from {source}") from exc
    if not isinstance(data, dict):
        raise AuthError(f"auth session in {source} is not an object")
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthError(f"auth session in {source} is missing access_token")
    expires_at = data.get("expires_at")
    return AuthSession(
        access_token=access_token,
        refresh_token=data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else None,
        expires_at=expires_at if isinstance(expires_at, int) else None,
        token_type=data.get("token_type") if isinstance(data.get("token_type"), str) else "bearer",
        supabase_url=data.get("supabase_url") if isinstance(data.get("supabase_url"), str) else None,
        supabase_anon_key=data.get("supabase_anon_key") if isinstance(data.get("supabase_anon_key"), str) else None,
        email=data.get("email") if isinstance(data.get("email"), str) else None,
        user_id=data.get("user_id") if isinstance(data.get("user_id"), str) else None,
        provider=data.get("provider") if isinstance(data.get("provider"), str) else None,
    )


def load_access_token() -> str | None:
    session = load_session()
    if session is None:
        return None
    if session.is_expired:
        session = refresh_session(session)
    return session.access_token


def clear_session(*, path: Path | None = None) -> bool:
    source = path or auth_file_path()
    if not source.exists():
        return False
    source.unlink()
    return True
