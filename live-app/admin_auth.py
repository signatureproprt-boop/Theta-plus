"""Separate credential-admin password; public trading screens stay keyless."""
import hashlib
import hmac
import os
from fastapi import HTTPException


def admin_config():
    return bool(os.environ.get('THETA_TOKEN_ADMIN_HASH','').strip())


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm!='pbkdf2_sha256' or rounds!='600000':return False
        if not 16<=len(salt)<=128 or len(expected)!=64:return False
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()
        return hmac.compare_digest(actual,expected)
    except (ValueError,TypeError):return False


def require_admin(request):
    encoded=os.environ.get('THETA_TOKEN_ADMIN_HASH','').strip()
    if not encoded:raise HTTPException(503,'Token admin password needs one-time setup')
    password=request.headers.get('x-theta-admin-password','')
    if not password or len(password)>512 or not verify_password(password,encoded):
        raise HTTPException(403,'Token admin password is incorrect')
    return 'token-admin'
