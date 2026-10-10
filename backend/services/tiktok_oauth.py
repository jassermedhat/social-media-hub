import hashlib
import hmac
import re
import secrets
import string
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from services.tiktok_credentials import (
    DpapiJsonFile,
    SecureStorageError,
)


TIKTOK_AUTHORIZATION_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
TIKTOK_REQUIRED_REDIRECT_URI = (
    "http://127.0.0.1:8000/accounts/tiktok/callback"
)
TIKTOK_REQUIRED_SCOPES = frozenset(
    {"user.info.basic", "video.publish", "video.upload"}
)
TIKTOK_STATE_TTL_SECONDS = 600
TIKTOK_REFRESH_WINDOW_SECONDS = 300

_PKCE_CHARACTERS = string.ascii_letters + string.digits + "-._~"
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class TikTokOAuthError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        log_id: str | None = None,
        reauthorization_required: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = _safe_error_value(error_code)
        self.log_id = _safe_error_value(log_id, maximum=200)
        self.reauthorization_required = reauthorization_required

    def safe_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"message": self.message}

        if self.error_code:
            detail["error"] = self.error_code

        if self.log_id:
            detail["log_id"] = self.log_id

        return detail


class TikTokStateError(TikTokOAuthError):
    pass


class TikTokScopeError(TikTokOAuthError):
    def __init__(self, missing_scopes: set[str]):
        self.missing_scopes = sorted(missing_scopes)
        super().__init__(
            "TikTok authorization did not grant all required scopes.",
            error_code="missing_required_scopes",
            reauthorization_required=True,
        )

    def safe_detail(self) -> dict[str, Any]:
        detail = super().safe_detail()
        detail["missing_scopes"] = self.missing_scopes
        return detail


class TikTokReauthorizationRequired(TikTokOAuthError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        log_id: str | None = None,
    ):
        super().__init__(
            message,
            error_code=error_code,
            log_id=log_id,
            reauthorization_required=True,
        )


def _safe_error_value(
    value: Any,
    *,
    maximum: int = 100,
) -> str | None:
    if not isinstance(value, str):
        return None

    value = value.strip()

    if not value or len(value) > maximum:
        return None

    if maximum == 100 and not _SAFE_ERROR_CODE.fullmatch(value):
        return None

    if maximum != 100 and not all(
        character.isalnum() or character in "-_."
        for character in value
    ):
        return None

    return value


def parse_scopes(value: str | list[str] | tuple[str, ...]) -> set[str]:
    if isinstance(value, str):
        items = value.replace(",", " ").split()
    else:
        items = value

    return {
        str(scope).strip()
        for scope in items
        if str(scope).strip()
    }


def generate_tiktok_pkce(length: int = 64) -> tuple[str, str]:
    if length < 43 or length > 128:
        raise ValueError("PKCE verifier length must be between 43 and 128.")

    code_verifier = "".join(
        secrets.choice(_PKCE_CHARACTERS)
        for _ in range(length)
    )
    code_challenge = hashlib.sha256(
        code_verifier.encode("ascii")
    ).hexdigest()
    return code_verifier, code_challenge


class TikTokOAuthStateStore:
    def __init__(
        self,
        secure_file: DpapiJsonFile,
        *,
        clock: Callable[[], float] = time.time,
        ttl_seconds: int = TIKTOK_STATE_TTL_SECONDS,
    ):
        self.secure_file = secure_file
        self.clock = clock
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _state_digest(state: str) -> str:
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    def _active_sessions(self) -> list[dict[str, Any]]:
        stored = self.secure_file.load() or {}
        sessions = stored.get("sessions") or []
        now = self.clock()

        return [
            session
            for session in sessions
            if isinstance(session, dict)
            and float(session.get("expires_at", 0)) > now
        ]

    def create(self, code_verifier: str) -> str:
        now = self.clock()
        state = secrets.token_urlsafe(32)
        sessions = self._active_sessions()
        sessions.append(
            {
                "state_digest": self._state_digest(state),
                "code_verifier": code_verifier,
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
            }
        )
        self.secure_file.save({"sessions": sessions[-10:]})
        return state

    def consume(self, state: str) -> dict[str, Any]:
        if not state:
            raise TikTokStateError("Missing TikTok OAuth state.")

        stored = self.secure_file.load()

        if not stored:
            raise TikTokStateError(
                "TikTok OAuth session was not found. Start connection again."
            )

        sessions = stored.get("sessions") or []
        supplied_digest = self._state_digest(state)
        now = self.clock()
        matched = None
        remaining = []
        expired_match = False

        for session in sessions:
            if not isinstance(session, dict):
                continue

            expected_digest = str(session.get("state_digest") or "")
            is_match = hmac.compare_digest(
                supplied_digest,
                expected_digest,
            )

            try:
                is_expired = float(session.get("expires_at", 0)) <= now
            except (TypeError, ValueError):
                is_expired = True

            if is_match:
                if is_expired:
                    expired_match = True
                else:
                    matched = session
                continue

            if not is_expired:
                remaining.append(session)

        if remaining:
            self.secure_file.save({"sessions": remaining})
        else:
            self.secure_file.delete()

        if expired_match:
            raise TikTokStateError(
                "TikTok OAuth session expired. Start connection again."
            )

        if matched is None:
            raise TikTokStateError("Invalid TikTok OAuth state.")

        verifier = matched.get("code_verifier")

        if not isinstance(verifier, str) or not verifier:
            raise TikTokStateError(
                "TikTok OAuth session is invalid. Start connection again."
            )

        return matched

    def clear(self) -> None:
        self.secure_file.delete()


class TikTokOAuthService:
    def __init__(
        self,
        settings: dict[str, Any],
        *,
        credential_store: DpapiJsonFile,
        state_store: TikTokOAuthStateStore,
        http_client=requests,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.credential_store = credential_store
        self.state_store = state_store
        self.http_client = http_client
        self.clock = clock

    def authorization_url(self) -> str:
        code_verifier, code_challenge = generate_tiktok_pkce()
        state = self.state_store.create(code_verifier)
        query = urlencode(
            {
                "client_key": self.settings["client_key"],
                "response_type": "code",
                "scope": ",".join(self.settings["scopes"]),
                "redirect_uri": self.settings["redirect_uri"],
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{TIKTOK_AUTHORIZATION_URL}?{query}"

    @staticmethod
    def _safe_json(response) -> dict[str, Any]:
        try:
            data = response.json()
        except (TypeError, ValueError):
            return {}

        return data if isinstance(data, dict) else {}

    def _oauth_response(self, response, context: str) -> dict[str, Any]:
        data = self._safe_json(response)
        error_code = data.get("error")

        if response.ok and not error_code:
            return data

        reauthorization_required = error_code in {
            "access_denied",
            "invalid_grant",
            "invalid_scope",
        }
        error_class = (
            TikTokReauthorizationRequired
            if reauthorization_required
            else TikTokOAuthError
        )
        raise error_class(
            context,
            error_code=error_code or f"http_{response.status_code}",
            log_id=data.get("log_id"),
        )

    def _api_response(self, response, context: str) -> dict[str, Any]:
        payload = self._safe_json(response)
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        error_code = error.get("code")

        if response.ok and error_code == "ok":
            data = payload.get("data")
            return data if isinstance(data, dict) else {}

        reauthorization_required = error_code in {
            "access_token_invalid",
            "scope_not_authorized",
        }
        error_class = (
            TikTokReauthorizationRequired
            if reauthorization_required
            else TikTokOAuthError
        )
        raise error_class(
            context,
            error_code=error_code or f"http_{response.status_code}",
            log_id=error.get("log_id"),
        )

    def _post_form(self, url: str, data: dict[str, Any]):
        try:
            return self.http_client.post(
                url,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cache-Control": "no-cache",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise TikTokOAuthError(
                "TikTok OAuth service could not be reached."
            ) from exc

    def _token_credentials(
        self,
        data: dict[str, Any],
        *,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(self.clock())
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        open_id = data.get("open_id")

        if not all(
            isinstance(value, str) and value
            for value in (access_token, refresh_token, open_id)
        ):
            raise TikTokOAuthError(
                "TikTok returned an incomplete credential response.",
                error_code="incomplete_token_response",
            )

        if previous and open_id != previous.get("open_id"):
            raise TikTokReauthorizationRequired(
                "TikTok refresh returned a different account identifier.",
                error_code="open_id_mismatch",
            )

        granted_scopes = parse_scopes(data.get("scope", ""))
        missing_scopes = TIKTOK_REQUIRED_SCOPES - granted_scopes

        if missing_scopes:
            raise TikTokScopeError(missing_scopes)

        try:
            expires_in = int(data.get("expires_in"))
            refresh_expires_in = int(data.get("refresh_expires_in"))
        except (TypeError, ValueError) as exc:
            raise TikTokOAuthError(
                "TikTok returned invalid credential expiration values.",
                error_code="invalid_token_expiration",
            ) from exc

        credentials = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "open_id": open_id,
            "scopes": sorted(granted_scopes),
            "token_type": str(data.get("token_type") or "Bearer"),
            "retrieved_at": now,
            "access_expires_at": now + expires_in,
            "refresh_expires_at": now + refresh_expires_in,
        }

        if previous and previous.get("display_name"):
            credentials["display_name"] = previous["display_name"]

        return credentials

    def _revoke_token_value(self, access_token: str) -> None:
        response = self._post_form(
            TIKTOK_REVOKE_URL,
            {
                "client_key": self.settings["client_key"],
                "client_secret": self.settings["client_secret"],
                "token": access_token,
            },
        )
        self._oauth_response(response, "TikTok token revocation failed.")

    def _fetch_user(self, access_token: str) -> dict[str, Any]:
        try:
            response = self.http_client.get(
                TIKTOK_USER_INFO_URL,
                params={"fields": "open_id,display_name"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise TikTokOAuthError(
                "TikTok user information service could not be reached."
            ) from exc

        data = self._api_response(
            response,
            "TikTok user information request failed.",
        )
        user = data.get("user")

        if not isinstance(user, dict):
            raise TikTokOAuthError(
                "TikTok did not return user information.",
                error_code="missing_user_info",
            )

        return user

    def complete_authorization(
        self,
        *,
        state: str | None,
        code: str | None,
        oauth_error: str | None = None,
    ) -> dict[str, Any]:
        session = self.state_store.consume(state or "")

        if oauth_error:
            raise TikTokOAuthError(
                "TikTok authorization was denied or could not be completed.",
                error_code=oauth_error,
            )

        if not code:
            raise TikTokOAuthError(
                "Missing TikTok authorization code.",
                error_code="missing_authorization_code",
            )

        response = self._post_form(
            TIKTOK_TOKEN_URL,
            {
                "client_key": self.settings["client_key"],
                "client_secret": self.settings["client_secret"],
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.settings["redirect_uri"],
                "code_verifier": session["code_verifier"],
            },
        )
        token_data = self._oauth_response(
            response,
            "TikTok authorization-code exchange failed.",
        )

        try:
            credentials = self._token_credentials(token_data)
        except TikTokScopeError:
            access_token = token_data.get("access_token")

            if isinstance(access_token, str) and access_token:
                try:
                    self._revoke_token_value(access_token)
                except TikTokOAuthError:
                    pass

            raise

        try:
            user = self._fetch_user(credentials["access_token"])

            if user.get("open_id") != credentials["open_id"]:
                raise TikTokOAuthError(
                    "TikTok user identity did not match the token account.",
                    error_code="open_id_mismatch",
                )

            display_name = user.get("display_name")

            if isinstance(display_name, str) and display_name.strip():
                credentials["display_name"] = display_name.strip()

            self.credential_store.save(credentials)
        except (TikTokOAuthError, SecureStorageError):
            try:
                self._revoke_token_value(credentials["access_token"])
            except TikTokOAuthError:
                pass
            raise

        return self.safe_account(credentials)

    def refresh_credentials(
        self,
        credentials: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._post_form(
            TIKTOK_TOKEN_URL,
            {
                "client_key": self.settings["client_key"],
                "client_secret": self.settings["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": credentials["refresh_token"],
            },
        )

        try:
            token_data = self._oauth_response(
                response,
                "TikTok access-token refresh failed.",
            )
            refreshed = self._token_credentials(
                token_data,
                previous=credentials,
            )
            self.credential_store.save(refreshed)
            return refreshed
        except TikTokReauthorizationRequired:
            self.credential_store.delete()
            raise
        except TikTokScopeError:
            self.credential_store.delete()
            raise TikTokReauthorizationRequired(
                "TikTok authorization no longer has the required scopes.",
                error_code="missing_required_scopes",
            )

    def valid_credentials(self) -> dict[str, Any]:
        credentials = self.credential_store.load()

        if not credentials:
            raise TikTokReauthorizationRequired(
                "TikTok is not connected.",
                error_code="not_connected",
            )

        now = self.clock()

        try:
            refresh_expires_at = float(
                credentials["refresh_expires_at"]
            )
            access_expires_at = float(
                credentials["access_expires_at"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            self.credential_store.delete()
            raise TikTokReauthorizationRequired(
                "TikTok credentials are invalid. Reconnect TikTok.",
                error_code="invalid_stored_credentials",
            ) from exc

        if refresh_expires_at <= now:
            self.credential_store.delete()
            raise TikTokReauthorizationRequired(
                "TikTok authorization expired. Reconnect TikTok.",
                error_code="refresh_token_expired",
            )

        if access_expires_at <= now + TIKTOK_REFRESH_WINDOW_SECONDS:
            return self.refresh_credentials(credentials)

        return credentials

    @staticmethod
    def safe_account(credentials: dict[str, Any]) -> dict[str, Any]:
        return {
            "account_id": credentials.get("open_id"),
            "account_name": (
                credentials.get("display_name")
                or credentials.get("open_id")
            ),
            "granted_scopes": list(credentials.get("scopes") or []),
            "access_expires_at": credentials.get("access_expires_at"),
            "refresh_expires_at": credentials.get("refresh_expires_at"),
        }

    def revoke(self) -> None:
        credentials = self.credential_store.load()

        if not credentials:
            raise TikTokReauthorizationRequired(
                "TikTok is not connected.",
                error_code="not_connected",
            )

        access_token = credentials.get("access_token")

        if not isinstance(access_token, str) or not access_token:
            raise TikTokReauthorizationRequired(
                "TikTok credentials are invalid. Reconnect TikTok.",
                error_code="invalid_stored_credentials",
            )

        self._revoke_token_value(access_token)

    def clear_local(self) -> None:
        self.credential_store.delete()
        self.state_store.clear()


def create_tiktok_service(
    settings: dict[str, Any],
    base_dir: Path,
) -> TikTokOAuthService:
    credential_store = DpapiJsonFile(
        base_dir / "tiktok_credentials.bin"
    )
    state_file = DpapiJsonFile(
        base_dir / "tiktok_oauth_state.bin"
    )
    return TikTokOAuthService(
        settings,
        credential_store=credential_store,
        state_store=TikTokOAuthStateStore(state_file),
    )
