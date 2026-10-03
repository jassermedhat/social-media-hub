import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from database.db import (
    get_connected_account,
    upsert_connected_account,
)

router = APIRouter(
    prefix="/accounts",
    tags=["Accounts"],
)

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

CLIENT_SECRET_FILE = BASE_DIR / "ips_client_secret.json"
TOKEN_FILE = BASE_DIR / "youtube_token.json"
OAUTH_STATE_FILE = BASE_DIR / "youtube_oauth_state.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

REDIRECT_URI = "http://127.0.0.1:8000/accounts/youtube/callback"

# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

DRIVE_CLIENT_SECRET_FILE = BASE_DIR / "ips_drive_client_secret.json"
DRIVE_TOKEN_FILE = BASE_DIR / "drive_token.json"
DRIVE_OAUTH_STATE_FILE = BASE_DIR / "drive_oauth_state.json"

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]

DRIVE_REDIRECT_URI = "http://127.0.0.1:8000/accounts/drive/callback"

@router.get("/youtube/connect")
def connect_youtube():
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=True,
    )

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )

    oauth_data = {
        "state": state,
        "code_verifier": flow.code_verifier,
    }

    OAUTH_STATE_FILE.write_text(
        json.dumps(oauth_data),
        encoding="utf-8",
    )

    return RedirectResponse(authorization_url)


@router.get("/youtube/callback")
def youtube_callback(request: Request):
    state = request.query_params.get("state")

    if not state:
        raise HTTPException(
            status_code=400,
            detail="Missing OAuth state.",
        )

    if not OAUTH_STATE_FILE.exists():
        raise HTTPException(
            status_code=400,
            detail="OAuth session not found. Start connection again.",
        )

    oauth_data = json.loads(
        OAUTH_STATE_FILE.read_text(
            encoding="utf-8",
        )
    )

    expected_state = oauth_data.get("state")

    if state != expected_state:
        raise HTTPException(
            status_code=400,
            detail="Invalid OAuth state.",
        )

    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        scopes=SCOPES,
        state=state,
        redirect_uri=REDIRECT_URI,
        code_verifier=oauth_data.get("code_verifier"),
    )

    flow.fetch_token(
        authorization_response=str(request.url),
    )

    credentials = flow.credentials

    youtube = build(
        "youtube",
        "v3",
        credentials=credentials,
    )

    channel_response = youtube.channels().list(
        part="snippet",
        mine=True,
    ).execute()

    channels = channel_response.get("items", [])

    if not channels:
        raise HTTPException(
            status_code=400,
            detail=(
                "No YouTube channel was found "
                "for this Google account."
            ),
        )

    channel = channels[0]

    channel_id = channel["id"]
    channel_name = channel["snippet"]["title"]

    TOKEN_FILE.write_text(
        credentials.to_json(),
        encoding="utf-8",
    )

    upsert_connected_account(
        platform="youtube",
        account_id=channel_id,
        account_name=channel_name,
        token_file=TOKEN_FILE.name,
    )

    OAUTH_STATE_FILE.unlink(missing_ok=True)

    return {
        "success": True,
        "platform": "youtube",
        "connected": True,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "message": "YouTube connected successfully.",
    }


@router.get("/youtube/status")
def youtube_status():
    account = get_connected_account("youtube")

    if not account:
        return {
            "platform": "youtube",
            "connected": False,
        }

    return {
        "platform": "youtube",
        "connected": TOKEN_FILE.exists(),
        "channel_id": account["account_id"],
        "channel_name": account["account_name"],
        "connected_at": account["connected_at"],
    }

# ---------------------------------------------------------------------------
# Google Drive OAuth
# ---------------------------------------------------------------------------


@router.get("/drive/connect")
def connect_drive():
    flow = Flow.from_client_secrets_file(
        str(DRIVE_CLIENT_SECRET_FILE),
        scopes=DRIVE_SCOPES,
        redirect_uri=DRIVE_REDIRECT_URI,
        autogenerate_code_verifier=True,
    )

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )

    oauth_data = {
        "state": state,
        "code_verifier": flow.code_verifier,
    }

    DRIVE_OAUTH_STATE_FILE.write_text(
        json.dumps(oauth_data),
        encoding="utf-8",
    )

    return RedirectResponse(authorization_url)

@router.get("/drive/callback")
def drive_callback(request: Request):
    state = request.query_params.get("state")

    if not state:
        raise HTTPException(
            status_code=400,
            detail="Missing Drive OAuth state.",
        )

    if not DRIVE_OAUTH_STATE_FILE.exists():
        raise HTTPException(
            status_code=400,
            detail="Drive OAuth session not found. Start connection again.",
        )

    oauth_data = json.loads(
        DRIVE_OAUTH_STATE_FILE.read_text(
            encoding="utf-8",
        )
    )

    expected_state = oauth_data.get("state")

    if state != expected_state:
        raise HTTPException(
            status_code=400,
            detail="Invalid Drive OAuth state.",
        )

    flow = Flow.from_client_secrets_file(
        str(DRIVE_CLIENT_SECRET_FILE),
        scopes=DRIVE_SCOPES,
        state=state,
        redirect_uri=DRIVE_REDIRECT_URI,
        code_verifier=oauth_data.get("code_verifier"),
    )

    flow.fetch_token(
        authorization_response=str(request.url),
    )

    credentials = flow.credentials

    DRIVE_TOKEN_FILE.write_text(
        credentials.to_json(),
        encoding="utf-8",
    )

    DRIVE_OAUTH_STATE_FILE.unlink(missing_ok=True)

    return {
        "success": True,
        "platform": "google_drive",
        "connected": True,
        "message": "Google Drive connected successfully.",
    } 

@router.get("/drive/status")
def drive_status():
    return {
        "platform": "google_drive",
        "connected": DRIVE_TOKEN_FILE.exists(),
    }

    
# ---------------------------------------------------------------------------
# Meta: Facebook Page + linked Instagram Professional account
# ---------------------------------------------------------------------------

META_TOKEN_FILE = BASE_DIR / "meta_token.json"
META_OAUTH_STATE_FILE = BASE_DIR / "meta_oauth_state.json"


def _meta_settings():
    settings = {
        "app_id": os.getenv("META_APP_ID"),
        "app_secret": os.getenv("META_APP_SECRET"),
        "config_id": os.getenv("META_CONFIG_ID"),
        "redirect_uri": os.getenv(
            "META_REDIRECT_URI",
            "http://localhost:8000/accounts/meta/callback",
        ),
        "graph_version": os.getenv("META_GRAPH_VERSION", "v26.0"),
        "facebook_page_id": os.getenv("META_FACEBOOK_PAGE_ID"),
    }

    missing = [
        name
        for name in ("app_id", "app_secret", "config_id")
        if not settings[name]
    ]

    if missing:
        raise HTTPException(
            status_code=500,
            detail=(
                "Missing Meta configuration: "
                + ", ".join(missing)
                + ". Check backend/.env and restart FastAPI."
            ),
        )

    return settings


def _meta_graph_url(settings, path: str) -> str:
    return (
        f"https://graph.facebook.com/"
        f"{settings['graph_version']}/{path.lstrip('/')}"
    )


def _meta_raise_for_error(response, context: str):
    try:
        data = response.json()
    except ValueError:
        data = None

    if response.ok and not (
        isinstance(data, dict) and data.get("error")
    ):
        return data

    detail = None

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            detail = error.get("message")

    if not detail:
        detail = response.text[:500] or f"HTTP {response.status_code}"

    raise HTTPException(
        status_code=400,
        detail=f"{context}: {detail}",
    )


def _get_managed_pages(settings, user_access_token: str):
    response = requests.get(
        _meta_graph_url(settings, "/me/accounts"),
        headers={
            "Authorization": f"Bearer {user_access_token}",
        },
        params={
            "fields": (
                "id,name,access_token,tasks,"
                "instagram_business_account"
            ),
            "limit": 100,
        },
        timeout=30,
    )

    data = _meta_raise_for_error(
        response,
        "Could not retrieve Facebook Pages",
    )

    return data.get("data", [])


def _select_meta_page(settings, pages):
    expected_page_id = settings.get("facebook_page_id")

    if expected_page_id:
        page = next(
            (
                item
                for item in pages
                if str(item.get("id")) == str(expected_page_id)
            ),
            None,
        )

        if not page:
            available = [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                }
                for item in pages
            ]

            raise HTTPException(
                status_code=400,
                detail={
                    "message": (
                        "The configured Facebook Page was not returned "
                        "by Meta. Make sure the authorizing Facebook user "
                        "has access to that Page."
                    ),
                    "configured_page_id": expected_page_id,
                    "available_pages": available,
                },
            )

        return page

    linked_pages = [
        item
        for item in pages
        if item.get("instagram_business_account")
    ]

    if len(linked_pages) == 1:
        return linked_pages[0]

    if not linked_pages:
        raise HTTPException(
            status_code=400,
            detail=(
                "No managed Facebook Page with a linked Instagram "
                "Professional account was returned by Meta."
            ),
        )

    raise HTTPException(
        status_code=400,
        detail={
            "message": (
                "Multiple Facebook Pages with linked Instagram accounts "
                "were returned. Set META_FACEBOOK_PAGE_ID in backend/.env "
                "to the Page this app should connect."
            ),
            "pages": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                }
                for item in linked_pages
            ],
        },
    )


@router.get("/meta/connect")
def connect_meta():
    settings = _meta_settings()

    state = secrets.token_urlsafe(32)

    META_OAUTH_STATE_FILE.write_text(
        json.dumps({"state": state}),
        encoding="utf-8",
    )

    # Facebook Login for Business gets its permissions from config_id.
    # Do not add a separate "scope" parameter here.
    query = urlencode(
        {
            "client_id": settings["app_id"],
            "redirect_uri": settings["redirect_uri"],
            "state": state,
            "response_type": "code",
            "override_default_response_type": "true",
            "config_id": settings["config_id"],
        }
    )

    authorization_url = (
        f"https://www.facebook.com/"
        f"{settings['graph_version']}/dialog/oauth?{query}"
    )

    return RedirectResponse(authorization_url)


@router.get("/meta/callback")
def meta_callback(request: Request):
    settings = _meta_settings()

    oauth_error = request.query_params.get("error")
    oauth_error_message = (
        request.query_params.get("error_description")
        or request.query_params.get("error_message")
    )

    if oauth_error:
        META_OAUTH_STATE_FILE.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=(
                "Meta authorization failed: "
                + (oauth_error_message or oauth_error)
            ),
        )

    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Missing Meta authorization code.",
        )

    if not state:
        raise HTTPException(
            status_code=400,
            detail="Missing Meta OAuth state.",
        )

    if not META_OAUTH_STATE_FILE.exists():
        raise HTTPException(
            status_code=400,
            detail="Meta OAuth session not found. Start connection again.",
        )

    oauth_data = json.loads(
        META_OAUTH_STATE_FILE.read_text(
            encoding="utf-8",
        )
    )

    if state != oauth_data.get("state"):
        raise HTTPException(
            status_code=400,
            detail="Invalid Meta OAuth state.",
        )

    token_response = requests.post(
        _meta_graph_url(settings, "/oauth/access_token"),
        data={
            "client_id": settings["app_id"],
            "client_secret": settings["app_secret"],
            "redirect_uri": settings["redirect_uri"],
            "code": code,
        },
        timeout=30,
    )

    token_data = _meta_raise_for_error(
        token_response,
        "Meta authorization-code exchange failed",
    )

    short_lived_token = token_data.get("access_token")

    if not short_lived_token:
        raise HTTPException(
            status_code=400,
            detail="Meta did not return an access token.",
        )

    long_lived_response = requests.post(
        _meta_graph_url(settings, "/oauth/access_token"),
        data={
            "grant_type": "fb_exchange_token",
            "client_id": settings["app_id"],
            "client_secret": settings["app_secret"],
            "fb_exchange_token": short_lived_token,
        },
        timeout=30,
    )

    long_lived_data = _meta_raise_for_error(
        long_lived_response,
        "Meta long-lived token exchange failed",
    )

    user_access_token = long_lived_data.get("access_token")

    if not user_access_token:
        raise HTTPException(
            status_code=400,
            detail="Meta did not return a long-lived access token.",
        )

    pages = _get_managed_pages(
        settings,
        user_access_token,
    )

    if not pages:
        raise HTTPException(
            status_code=400,
            detail=(
                "Meta returned no Facebook Pages for this user. "
                "The authorizing Facebook account must have access "
                "to the IPS Automation Page."
            ),
        )

    page = _select_meta_page(
        settings,
        pages,
    )

    page_id = str(page["id"])
    page_name = page.get("name") or page_id
    page_access_token = page.get("access_token")
    instagram_account = page.get("instagram_business_account") or {}
    instagram_id = instagram_account.get("id")

    if not page_access_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "Meta returned the Facebook Page but did not return "
                "a Page access token."
            ),
        )

    if not instagram_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "The selected Facebook Page does not have a linked "
                "Instagram Professional account."
            ),
        )

    instagram_response = requests.get(
        _meta_graph_url(settings, f"/{instagram_id}"),
        headers={
            "Authorization": f"Bearer {page_access_token}",
        },
        params={
            "fields": "id,username,name",
        },
        timeout=30,
    )

    instagram_data = _meta_raise_for_error(
        instagram_response,
        "Could not retrieve the linked Instagram account",
    )

    instagram_username = (
        instagram_data.get("username")
        or instagram_data.get("name")
        or str(instagram_id)
    )

    token_payload = {
        "graph_api_version": settings["graph_version"],
        "user_access_token": user_access_token,
        "user_token_expires_in": long_lived_data.get("expires_in"),
        "facebook_page_id": page_id,
        "facebook_page_name": page_name,
        "page_access_token": page_access_token,
        "instagram_business_account_id": str(instagram_id),
        "instagram_username": instagram_username,
    }

    META_TOKEN_FILE.write_text(
        json.dumps(token_payload, indent=2),
        encoding="utf-8",
    )

    upsert_connected_account(
        platform="facebook",
        account_id=page_id,
        account_name=page_name,
        token_file=META_TOKEN_FILE.name,
    )

    upsert_connected_account(
        platform="instagram",
        account_id=str(instagram_id),
        account_name=instagram_username,
        token_file=META_TOKEN_FILE.name,
    )

    META_OAUTH_STATE_FILE.unlink(missing_ok=True)

    return {
        "success": True,
        "connected": True,
        "facebook": {
            "page_id": page_id,
            "page_name": page_name,
        },
        "instagram": {
            "account_id": str(instagram_id),
            "username": instagram_username,
        },
        "message": (
            "Meta connected successfully. Facebook Page and linked "
            "Instagram account were saved."
        ),
    }


@router.get("/meta/status")
def meta_status():
    facebook_account = get_connected_account("facebook")
    instagram_account = get_connected_account("instagram")

    return {
        "connected": (
            META_TOKEN_FILE.exists()
            and facebook_account is not None
            and instagram_account is not None
        ),
        "facebook": (
            {
                "connected": META_TOKEN_FILE.exists(),
                "page_id": facebook_account["account_id"],
                "page_name": facebook_account["account_name"],
                "connected_at": facebook_account["connected_at"],
            }
            if facebook_account
            else {
                "connected": False,
            }
        ),
        "instagram": (
            {
                "connected": META_TOKEN_FILE.exists(),
                "account_id": instagram_account["account_id"],
                "username": instagram_account["account_name"],
                "connected_at": instagram_account["connected_at"],
            }
            if instagram_account
            else {
                "connected": False,
            }
        ),
    }

