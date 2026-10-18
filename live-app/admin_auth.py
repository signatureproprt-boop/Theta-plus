"""Google ID-token verification for credential administration only."""
import os
from fastapi import HTTPException
from google.oauth2 import id_token
from google.auth.transport.requests import Request as GoogleRequest


def admin_config():
    client = os.environ.get('THETA_GOOGLE_CLIENT_ID', '').strip()
    emails = {e.strip().lower() for e in os.environ.get('THETA_ADMIN_EMAILS', '').split(',') if e.strip()}
    return client, emails


def require_admin(request):
    client, emails = admin_config()
    if not client or not emails:
        raise HTTPException(503, 'Google admin sign-in needs one-time setup')
    credential = request.headers.get('x-theta-admin-id-token', '')
    if not credential or len(credential) > 16384:
        raise HTTPException(403, 'Sign in with your authorised Google admin account')
    try:
        claims = id_token.verify_oauth2_token(credential, GoogleRequest(), audience=client)
    except Exception:
        raise HTTPException(403, 'Google admin sign-in expired or invalid; sign in again') from None
    email = claims.get('email', '').lower()
    authoritative = email.endswith('@gmail.com') or bool(claims.get('hd'))
    if not claims.get('sub') or claims.get('email_verified') is not True or not authoritative or email not in emails:
        raise HTTPException(403, 'This Google account is not an authorised administrator')
    return claims['sub']
