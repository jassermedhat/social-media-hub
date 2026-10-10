import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from api import accounts
from services.tiktok_credentials import DpapiJsonFile
from services.tiktok_oauth import (
    TIKTOK_REQUIRED_REDIRECT_URI,
    TikTokOAuthError,
    TikTokOAuthService,
    TikTokOAuthStateStore,
    TikTokReauthorizationRequired,
    TikTokScopeError,
    TikTokStateError,
    generate_tiktok_pkce,
)


class MemorySecureFile:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data)

    def exists(self):
        return self.data is not None

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, data):
        self.data = copy.deepcopy(data)

    def delete(self):
        self.data = None


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.payload = payload
        self.text = text

    def json(self):
        if self.payload is None:
            raise ValueError("No JSON body")
        return copy.deepcopy(self.payload)


class FakeHttp:
    def __init__(self, *, posts=None, gets=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, copy.deepcopy(kwargs)))
        if not self.posts:
            raise AssertionError("Unexpected POST request")
        return self.posts.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, copy.deepcopy(kwargs)))
        if not self.gets:
            raise AssertionError("Unexpected GET request")
        return self.gets.pop(0)


def settings():
    return {
        "client_key": "test-client-key",
        "client_secret": "test-client-secret",
        "redirect_uri": TIKTOK_REQUIRED_REDIRECT_URI,
        "scopes": [
            "user.info.basic",
            "video.publish",
            "video.upload",
        ],
        "environment": "sandbox",
        "direct_post_enabled": False,
        "public_posting_enabled": False,
        "configured": True,
        "configuration_issues": [],
    }


def token_payload(**overrides):
    payload = {
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "open_id": "open-id-123",
        "scope": "user.info.basic,video.publish,video.upload",
        "token_type": "Bearer",
        "expires_in": 86400,
        "refresh_expires_in": 31536000,
    }
    payload.update(overrides)
    return payload


def stored_credentials(**overrides):
    payload = {
        "access_token": "stored-access-token",
        "refresh_token": "stored-refresh-token",
        "open_id": "open-id-123",
        "display_name": "Sandbox User",
        "scopes": [
            "user.info.basic",
            "video.publish",
            "video.upload",
        ],
        "token_type": "Bearer",
        "retrieved_at": 900,
        "access_expires_at": 2000,
        "refresh_expires_at": 5000,
    }
    payload.update(overrides)
    return payload


class TikTokOAuthTests(unittest.TestCase):
    def make_service(
        self,
        *,
        now=1000,
        http=None,
        credentials=None,
        state_file=None,
        ttl=600,
    ):
        credential_store = MemorySecureFile(credentials)
        state_file = state_file or MemorySecureFile()
        state_store = TikTokOAuthStateStore(
            state_file,
            clock=lambda: now,
            ttl_seconds=ttl,
        )
        service = TikTokOAuthService(
            settings(),
            credential_store=credential_store,
            state_store=state_store,
            http_client=http or FakeHttp(),
            clock=lambda: now,
        )
        return service, credential_store, state_store

    def test_pkce_uses_valid_verifier_and_hex_sha256(self):
        verifier, challenge = generate_tiktok_pkce()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)
        self.assertEqual(
            challenge,
            hashlib.sha256(verifier.encode("ascii")).hexdigest(),
        )
        self.assertRegex(challenge, r"^[0-9a-f]{64}$")

    def test_authorization_url_contains_desktop_pkce_and_scopes(self):
        service, _, _ = self.make_service()
        url = service.authorization_url()
        query = parse_qs(urlparse(url).query)

        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(
            query["redirect_uri"],
            [TIKTOK_REQUIRED_REDIRECT_URI],
        )
        self.assertEqual(
            set(query["scope"][0].split(",")),
            {"user.info.basic", "video.publish", "video.upload"},
        )

    def test_state_mismatch_expiration_and_replay(self):
        state_file = MemorySecureFile()
        current_time = [1000]
        state_store = TikTokOAuthStateStore(
            state_file,
            clock=lambda: current_time[0],
            ttl_seconds=10,
        )
        state = state_store.create("verifier-one")

        with self.assertRaises(TikTokStateError):
            state_store.consume("wrong-state")

        consumed = state_store.consume(state)
        self.assertEqual(consumed["code_verifier"], "verifier-one")

        with self.assertRaises(TikTokStateError):
            state_store.consume(state)

        expired = state_store.create("verifier-two")
        current_time[0] = 1011

        with self.assertRaisesRegex(TikTokStateError, "expired"):
            state_store.consume(expired)

    def test_authorization_denial_consumes_state(self):
        service, _, state_store = self.make_service()
        state = state_store.create("verifier")

        with self.assertRaises(TikTokOAuthError) as context:
            service.complete_authorization(
                state=state,
                code=None,
                oauth_error="access_denied",
            )

        self.assertEqual(context.exception.error_code, "access_denied")

        with self.assertRaises(TikTokStateError):
            state_store.consume(state)

    def test_missing_authorization_code_is_rejected(self):
        service, _, state_store = self.make_service()
        state = state_store.create("verifier")

        with self.assertRaises(TikTokOAuthError) as context:
            service.complete_authorization(state=state, code=None)

        self.assertEqual(
            context.exception.error_code,
            "missing_authorization_code",
        )

    def test_invalid_authorization_code_is_safely_reported(self):
        secret_body = "authorization-code-should-not-leak"
        http = FakeHttp(
            posts=[
                FakeResponse(
                    400,
                    {
                        "error": "invalid_grant",
                        "error_description": secret_body,
                        "log_id": "safe-log-id",
                    },
                    text=secret_body,
                )
            ]
        )
        service, _, state_store = self.make_service(http=http)
        state = state_store.create("verifier")

        with self.assertRaises(TikTokReauthorizationRequired) as context:
            service.complete_authorization(
                state=state,
                code="invalid-code",
            )

        rendered = json.dumps(context.exception.safe_detail())
        self.assertNotIn(secret_body, rendered)
        self.assertNotIn("invalid-code", rendered)
        self.assertEqual(context.exception.error_code, "invalid_grant")

    def test_partial_scope_grant_is_rejected_and_not_stored(self):
        http = FakeHttp(
            posts=[
                FakeResponse(
                    payload=token_payload(scope="user.info.basic")
                ),
                FakeResponse(payload=None),
            ]
        )
        service, credential_store, state_store = self.make_service(http=http)
        state = state_store.create("verifier")

        with self.assertRaises(TikTokScopeError) as context:
            service.complete_authorization(state=state, code="valid-code")

        self.assertEqual(
            set(context.exception.missing_scopes),
            {"video.publish", "video.upload"},
        )
        self.assertFalse(credential_store.exists())
        self.assertEqual(len(http.post_calls), 2)

    def test_token_exchange_saves_safe_identity_and_credentials(self):
        http = FakeHttp(
            posts=[FakeResponse(payload=token_payload())],
            gets=[
                FakeResponse(
                    payload={
                        "data": {
                            "user": {
                                "open_id": "open-id-123",
                                "display_name": "Sandbox User",
                            }
                        },
                        "error": {
                            "code": "ok",
                            "message": "",
                            "log_id": "log-id",
                        },
                    }
                )
            ],
        )
        service, credential_store, state_store = self.make_service(http=http)
        state = state_store.create("verifier")
        account = service.complete_authorization(
            state=state,
            code="valid-code",
        )

        self.assertEqual(account["account_id"], "open-id-123")
        self.assertEqual(account["account_name"], "Sandbox User")
        self.assertNotIn("access_token", account)
        self.assertEqual(
            credential_store.data["access_token"],
            "access-token-value",
        )
        token_call = http.post_calls[0][1]["data"]
        self.assertEqual(token_call["grant_type"], "authorization_code")
        self.assertEqual(token_call["redirect_uri"], TIKTOK_REQUIRED_REDIRECT_URI)
        self.assertIn("code_verifier", token_call)

    def test_refresh_rotates_refresh_token_atomically(self):
        http = FakeHttp(
            posts=[
                FakeResponse(
                    payload=token_payload(
                        access_token="rotated-access-token",
                        refresh_token="rotated-refresh-token",
                    )
                )
            ]
        )
        service, credential_store, _ = self.make_service(
            http=http,
            credentials=stored_credentials(access_expires_at=1001),
        )
        refreshed = service.valid_credentials()

        self.assertEqual(
            refreshed["refresh_token"],
            "rotated-refresh-token",
        )
        self.assertEqual(
            credential_store.data["access_token"],
            "rotated-access-token",
        )
        self.assertEqual(
            http.post_calls[0][1]["data"]["refresh_token"],
            "stored-refresh-token",
        )

    def test_expired_refresh_token_requires_reauthorization(self):
        service, credential_store, _ = self.make_service(
            credentials=stored_credentials(refresh_expires_at=999),
        )

        with self.assertRaises(TikTokReauthorizationRequired) as context:
            service.valid_credentials()

        self.assertEqual(
            context.exception.error_code,
            "refresh_token_expired",
        )
        self.assertFalse(credential_store.exists())

    def test_revoked_refresh_token_requires_reauthorization(self):
        secret_description = "refresh-token-value-must-not-leak"
        http = FakeHttp(
            posts=[
                FakeResponse(
                    400,
                    {
                        "error": "invalid_grant",
                        "error_description": secret_description,
                    },
                    text=secret_description,
                )
            ]
        )
        service, credential_store, _ = self.make_service(
            http=http,
            credentials=stored_credentials(access_expires_at=1001),
        )

        with self.assertRaises(TikTokReauthorizationRequired) as context:
            service.valid_credentials()

        self.assertFalse(credential_store.exists())
        self.assertNotIn(secret_description, str(context.exception.safe_detail()))

    def test_dpapi_json_file_encrypts_and_atomically_round_trips(self):
        def protect(value):
            return b"DPAPI" + value[::-1]

        def unprotect(value):
            self.assertTrue(value.startswith(b"DPAPI"))
            return value[5:][::-1]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.bin"
            store = DpapiJsonFile(
                path,
                protect=protect,
                unprotect=unprotect,
            )
            store.save({"access_token": "sensitive-access-token"})

            self.assertNotIn(b"sensitive-access-token", path.read_bytes())
            self.assertEqual(
                store.load()["access_token"],
                "sensitive-access-token",
            )
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_account_status_never_returns_tokens(self):
        class StatusService:
            def valid_credentials(self):
                return stored_credentials()

            def safe_account(self, credentials):
                return TikTokOAuthService.safe_account(credentials)

        configured = settings()
        database_account = {
            "account_id": "open-id-123",
            "account_name": "Sandbox User",
            "connected_at": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch.object(accounts, "_tiktok_settings", return_value=configured),
            patch.object(accounts, "_tiktok_service", return_value=StatusService()),
            patch.object(
                accounts,
                "get_connected_account",
                return_value=database_account,
            ),
        ):
            response = accounts.tiktok_status()

        rendered = json.dumps(response)
        self.assertTrue(response["connected"])
        self.assertEqual(response["environment"], "sandbox")
        self.assertNotIn("stored-access-token", rendered)
        self.assertNotIn("stored-refresh-token", rendered)
        self.assertNotIn("access_token", rendered)
        self.assertNotIn("refresh_token", rendered)

    def test_disconnect_reports_revocation_error_and_supports_force_local(self):
        class ExistingStore:
            @staticmethod
            def exists():
                return True

        class DisconnectService:
            credential_store = ExistingStore()

            def __init__(self):
                self.cleared = False

            def revoke(self):
                raise TikTokOAuthError(
                    "TikTok token revocation failed.",
                    error_code="invalid_grant",
                )

            def clear_local(self):
                self.cleared = True

        service = DisconnectService()
        configured = settings()

        with (
            patch.object(accounts, "_tiktok_settings", return_value=configured),
            patch.object(accounts, "_tiktok_service", return_value=service),
            patch.object(
                accounts,
                "get_connected_account",
                return_value={"account_id": "open-id-123"},
            ),
            patch.object(accounts, "delete_connected_account") as delete,
        ):
            failed = accounts.disconnect_tiktok(force_local=False)
            payload = json.loads(failed.body)
            self.assertEqual(failed.status_code, 502)
            self.assertFalse(payload["disconnected"])
            self.assertFalse(service.cleared)
            delete.assert_not_called()

            forced = accounts.disconnect_tiktok(force_local=True)
            self.assertTrue(forced["disconnected"])
            self.assertFalse(forced["remote_revoked"])
            self.assertTrue(forced["local_credentials_removed"])
            self.assertTrue(service.cleared)
            delete.assert_called_once_with("tiktok")

    def test_disconnect_revokes_before_removing_local_credentials(self):
        events = []

        class ExistingStore:
            @staticmethod
            def exists():
                return True

        class DisconnectService:
            credential_store = ExistingStore()

            @staticmethod
            def revoke():
                events.append("revoked")

            @staticmethod
            def clear_local():
                events.append("cleared")

        with (
            patch.object(accounts, "_tiktok_settings", return_value=settings()),
            patch.object(
                accounts,
                "_tiktok_service",
                return_value=DisconnectService(),
            ),
            patch.object(
                accounts,
                "get_connected_account",
                return_value={"account_id": "open-id-123"},
            ),
            patch.object(accounts, "delete_connected_account") as delete,
        ):
            response = accounts.disconnect_tiktok()

        self.assertEqual(events, ["revoked", "cleared"])
        self.assertTrue(response["disconnected"])
        self.assertTrue(response["remote_revoked"])
        self.assertTrue(response["local_credentials_removed"])
        delete.assert_called_once_with("tiktok")


if __name__ == "__main__":
    unittest.main()
