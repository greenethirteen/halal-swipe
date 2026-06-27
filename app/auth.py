import base64
import hashlib
import hmac
import os
from typing import Optional

from fastapi import Request

from .database import one
from .settings import get_settings


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(salt + digest).decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        raw = base64.b64decode(stored_hash.encode("ascii"))
        salt, digest = raw[:16], raw[16:]
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def current_user(request: Request) -> Optional[dict]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    row = one("SELECT * FROM users WHERE id = ?", (user_id,))
    return dict(row) if row else None


def is_admin(user: Optional[dict]) -> bool:
    return bool(user and user.get("role") == "admin")


def has_active_subscription(user: Optional[dict]) -> bool:
    return bool(user and user.get("subscription_status") in {"active", "trialing"})


def role_for_email(email: str) -> str:
    return "admin" if email.lower().strip() == get_settings().admin_email.lower().strip() else "user"
