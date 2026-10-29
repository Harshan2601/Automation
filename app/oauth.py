from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import (
    LINKEDIN_CLIENT_ID,
    LINKEDIN_CLIENT_SECRET,
    LINKEDIN_REDIRECT_URI,
    PUBLIC_BASE_URL,
)
from app.db import (
    consume_connect_code,
    consume_oauth_state,
    create_oauth_state,
    get_customer_by_id,
    get_oauth_state_customer,
    update_linkedin_credentials,
)

router = APIRouter()
AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"


def _require_oauth_config() -> None:
    if not (PUBLIC_BASE_URL and LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET and LINKEDIN_REDIRECT_URI):
        raise HTTPException(
            status_code=503,
            detail="LinkedIn OAuth is not configured. Set PUBLIC_BASE_URL, LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET, and LINKEDIN_REDIRECT_URI.",
        )


@router.get("/linkedin/connect")
async def linkedin_connect(code: str = Query(..., min_length=20, max_length=200)):
    _require_oauth_config()
    customer_id = consume_connect_code(code)
    if not customer_id:
        raise HTTPException(status_code=400, detail="This connection link is invalid, expired, or already used. Request a fresh link from Telegram.")
    state = create_oauth_state(customer_id)
    params = {
        "response_type": "code",
        "client_id": LINKEDIN_CLIENT_ID,
        "redirect_uri": LINKEDIN_REDIRECT_URI,
        "state": state,
        "scope": "openid profile w_member_social",
    }
    return RedirectResponse(f"{AUTHORIZE_URL}?{urlencode(params)}", status_code=302)


@router.get("/linkedin/callback", response_class=HTMLResponse)
async def linkedin_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    _require_oauth_config()
    if error:
        raise HTTPException(status_code=400, detail=f"LinkedIn authorization was not completed: {error_description or error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing LinkedIn authorization code or state")
    customer_id = get_oauth_state_customer(state)
    if not customer_id:
        raise HTTPException(status_code=400, detail="OAuth state is invalid, expired, or already used. Start a new connection from Telegram.")

    async with httpx.AsyncClient(timeout=30) as client:
        token_response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": LINKEDIN_REDIRECT_URI,
                "client_id": LINKEDIN_CLIENT_ID,
                "client_secret": LINKEDIN_CLIENT_SECRET,
            },
        )
        if token_response.is_error:
            raise HTTPException(status_code=502, detail="LinkedIn token exchange failed. Check the registered redirect URI and app permissions.")
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        expires_in = int(token_data.get("expires_in", 0))
        if not access_token or expires_in <= 0:
            raise HTTPException(status_code=502, detail="LinkedIn returned an invalid token response")
        info_response = await client.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_response.is_error:
            raise HTTPException(status_code=502, detail="Could not read the authorized LinkedIn member identity")
        user_info = info_response.json()

    member_id = user_info.get("sub")
    if not member_id:
        raise HTTPException(status_code=502, detail="LinkedIn userinfo response did not include a member identifier")
    member_urn = member_id if str(member_id).startswith("urn:li:person:") else f"urn:li:person:{member_id}"
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    update_linkedin_credentials(
        customer_id=customer_id,
        member_urn=member_urn,
        access_token=access_token,
        refresh_token=token_data.get("refresh_token"),
        expires_at=expiry,
    )
    consume_oauth_state(state)
    customer = get_customer_by_id(customer_id)
    label = (customer or {}).get("display_name", "your customer")
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>LinkedIn connected</title></head>"
        "<body style='font-family:system-ui;max-width:680px;margin:4rem auto;padding:1rem'>"
        f"<h1>LinkedIn connected</h1><p>The LinkedIn account was connected to {label}. You can return to Telegram.</p>"
        "<p>The automation still requires explicit approval for every post.</p></body></html>"
    )
