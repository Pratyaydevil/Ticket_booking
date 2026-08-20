"""Auth endpoints: register (customer/organiser), login, current profile."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import create_token, get_current_user, hash_password, verify_password
from ..database import get_db
from ..models import User
from ..schemas import LoginIn, RegisterIn

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _public(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email,
            "role": user.role}


@router.post("/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    if db.query(User.id).filter_by(email=body.email.lower()).first():
        raise HTTPException(400, "An account with this email already exists")
    user = User(name=body.name, email=body.email.lower(),
                password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": create_token(user), "user": _public(user)}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=body.email.lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    return {"token": create_token(user), "user": _public(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return _public(user)
