"""
Authentication & role-based authorisation.

- Passwords: PBKDF2-HMAC-SHA256 with a per-user random salt (stdlib only,
  stored as "salt$hash" hex — easy to explain, no extra dependency).
- Sessions: stateless JWT carrying user id + role, signed with SECRET_KEY.
- require_role(...): dependency factory that enforces customer/organiser/admin.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import JWT_ALGORITHM, JWT_EXPIRE_MINUTES, SECRET_KEY
from .database import get_db
from .models import User

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()
    return secrets.compare_digest(candidate, digest)  # constant-time compare


def create_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)


def _user_from_request(request: Request, db: Session) -> Optional[User]:
    """Parse 'Authorization: Bearer <jwt>' and load the user, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    return db.get(User, int(payload["sub"]))


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Hard requirement: 401 if not logged in."""
    user = _user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def get_current_user_optional(request: Request,
                              db: Session = Depends(get_db)) -> Optional[User]:
    """Soft requirement: used by the public seat map to mark 'my' held seats."""
    return _user_from_request(request, db)


def require_role(*roles: str):
    """Usage: user = Depends(require_role(Role.ADMIN)) -> 403 on wrong role."""
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403,
                                detail=f"Requires role: {' or '.join(roles)}")
        return user
    return checker
