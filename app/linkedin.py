from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from app.config import (
    LINKEDIN_API_VERSION,
    LINKEDIN_CLIENT_ID,
    LINKEDIN_CLIENT_SECRET,
)
from app.crypto import decrypt_secret
from app.db import update_linkedin_credentials

LINKEDIN_API_ROOT = "https://api.linkedin.com/rest"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def get_valid_access_token(customer: dict) -> str:
    token = decrypt_secret(customer.get("linkedin_access_token_enc"))
    if not token or not customer.get("linkedin_member_urn"):
        raise RuntimeError("This customer has not connected a LinkedIn account. Use /connect_linkedin first.")
    expires_at = _parse_iso(customer.get("linkedin_token_expires_at"))
    now = datetime.now(timezone.utc)
    if not expires_at or expires_at > now + timedelta(minutes=5):
        return token

    refresh_token = decrypt_secret(customer.get("linkedin_refresh_token_enc"))
    if not refresh_token:
        raise RuntimeError("LinkedIn access token is expired or expiring. Reconnect using /connect_linkedin.")
    if not LINKEDIN_CLIENT_ID or not LINKEDIN_CLIENT_SECRET:
        raise RuntimeError("LinkedIn OAuth is not configured. Reconnect after configuring the LinkedIn app.")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            LINKEDIN_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": LINKEDIN_CLIENT_ID,
                "client_secret": LINKEDIN_CLIENT_SECRET,
            },
        )
        if response.is_error:
            raise RuntimeError("LinkedIn token refresh failed. Reconnect using /connect_linkedin.")
        data = response.json()

    new_access = data.get("access_token")
    expires_in = int(data.get("expires_in", 0))
    if not new_access or expires_in <= 0:
        raise RuntimeError("LinkedIn returned an invalid token-refresh response. Reconnect the account.")
    new_refresh = data.get("refresh_token") or refresh_token
    new_expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    update_linkedin_credentials(
        customer_id=customer["id"],
        member_urn=customer["linkedin_member_urn"],
        access_token=new_access,
        refresh_token=new_refresh,
        expires_at=new_expiry,
    )
    return new_access


async def _upload_image(client: httpx.AsyncClient, token: str, member_urn: str, image_path: Path) -> str:
    headers = _api_headers(token)
    init_response = await client.post(
        f"{LINKEDIN_API_ROOT}/images?action=initializeUpload",
        headers=headers,
        json={"initializeUploadRequest": {"owner": member_urn}},
    )
    if init_response.is_error:
        raise RuntimeError(f"LinkedIn image initialization failed ({init_response.status_code}). Check API permissions.")
    value = init_response.json().get("value", {})
    upload_url = value.get("uploadUrl")
    image_urn = value.get("image")
    if not upload_url or not image_urn:
        raise RuntimeError("LinkedIn did not return an image upload URL and URN")

    # Do not send OAuth tokens to unexpected hosts if an API response is malformed.
    parsed = urlparse(upload_url)
    if parsed.scheme != "https" or not (parsed.hostname == "linkedin.com" or (parsed.hostname or "").endswith(".linkedin.com")):
        raise RuntimeError("LinkedIn returned an unexpected image upload host")
    content_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(image_path.suffix.lower(), "application/octet-stream")
    with image_path.open("rb") as image_file:
        upload_response = await client.put(
            upload_url,
            content=image_file.read(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        )
    if upload_response.is_error:
        raise RuntimeError(f"LinkedIn image upload failed ({upload_response.status_code})")
    return image_urn


async def publish_to_linkedin(customer: dict, post: dict) -> str:
    """Publish one already-approved post. The caller must enforce status + due time."""
    token = await get_valid_access_token(customer)
    member_urn = customer["linkedin_member_urn"]
    image_path = Path(post["photo_path"]).resolve()
    if not image_path.is_file():
        raise RuntimeError("The selected customer photo no longer exists")
    if len(post["draft_text"]) > 3000:
        raise RuntimeError("Draft is longer than LinkedIn's typical 3,000-character post limit; edit and re-approve it")

    async with httpx.AsyncClient(timeout=60) as client:
        image_urn = await _upload_image(client, token, member_urn, image_path)
        payload = {
            "author": member_urn,
            "commentary": post["draft_text"],
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
            "content": {"media": {"title": image_path.stem[:200], "id": image_urn}},
        }
        response = await client.post(
            f"{LINKEDIN_API_ROOT}/posts",
            headers=_api_headers(token),
            json=payload,
        )
        if response.is_error:
            body = response.text[:500]
            raise RuntimeError(f"LinkedIn post creation failed ({response.status_code}): {body}")
        return response.headers.get("x-restli-id", "created-no-id-returned")
