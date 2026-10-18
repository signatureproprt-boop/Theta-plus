"""Validate candidate credentials with Dhan profile before saving; never expose profile."""
import httpx
from fastapi import HTTPException


async def check_token(token, client_id):
    if not token or any(c.isspace() for c in token):
        raise HTTPException(422, 'Paste one complete token without spaces')
    if not client_id:
        raise HTTPException(503, 'Permanent Dhan Client ID is not configured')
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get('https://api.dhan.co/v2/profile',
                headers={'access-token': token, 'dhanClientId': client_id})
        if response.status_code in (401, 403):
            raise HTTPException(422, 'Dhan rejected this token; it may be expired or invalid. Existing token was not changed.')
        if response.status_code != 200:
            raise HTTPException(502, f'Dhan check failed (HTTP {response.status_code}); try again. Existing token was not changed.')
        profile = response.json()
        if not isinstance(profile, dict) or str(profile.get('dhanClientId', '')) != client_id:
            raise HTTPException(422, 'Token does not match the permanent Dhan Client ID. Existing token was not changed.')
        # Only provider-reported non-secret status, never the token or full profile.
        def short(key):
            value = profile.get(key)
            return str(value)[:100] if isinstance(value, (str, int, float)) else None
        return {'status': 'AUTHENTICATED', 'token_validity_reported': short('tokenValidity'),
                'data_plan': short('dataPlan'), 'data_validity_reported': short('dataValidity'),
                'active_segments': short('activeSegment'),
                'market_data_verified': False}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'Dhan connection check failed; existing token was not changed') from None
