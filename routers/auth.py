"""
routers/auth.py — Account registration, login sessions, and the
get_current_user / get_optional_user dependencies used to scope data per user.

Sessions are random tokens stored in the user_sessions table and delivered as
an HttpOnly cookie. Passwords are PBKDF2-SHA256 (stdlib only, no new deps).

The FIRST account ever registered adopts all pre-existing single-user data
(jobs / resumes / generated resumes with user_id IS NULL).
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import GeneratedResume, Job, Resume, User, UserSession, get_db

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE = "session_token"
SESSION_DAYS = 30
_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, AttributeError):
        return False


def _session_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sess = db.query(UserSession).filter(UserSession.token == token).first()
    if not sess:
        return None
    if sess.expires_at < datetime.utcnow():
        db.delete(sess)
        db.commit()
        return None
    return db.query(User).filter(User.id == sess.user_id).first()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = _session_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return _session_user(request, db)


def _start_session(response: Response, db: Session, user: User) -> None:
    token = secrets.token_urlsafe(32)
    db.add(UserSession(
        token=token,
        user_id=user.id,
        expires_at=datetime.utcnow() + timedelta(days=SESSION_DAYS),
    ))
    db.commit()
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="lax",
    )


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=8, max_length=200)


@router.post("/register")
def register(creds: Credentials, response: Response, db: Session = Depends(get_db)):
    username = creds.username.strip().lower()
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="Username already taken.")

    first_user = db.query(User).count() == 0
    user = User(username=username, password_hash=hash_password(creds.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    if first_user:
        # Adopt all data created before accounts existed
        for model in (Job, Resume, GeneratedResume):
            db.query(model).filter(model.user_id.is_(None)).update({"user_id": user.id})
        db.commit()

    _start_session(response, db, user)
    return {"id": user.id, "username": user.username, "adopted_existing_data": first_user}


@router.post("/login")
def login(creds: Credentials, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == creds.username.strip().lower()).first()
    if not user or not verify_password(creds.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    _start_session(response, db, user)
    return {"id": user.id, "username": user.username}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.query(UserSession).filter(UserSession.token == token).delete()
        db.commit()
    response.delete_cookie(SESSION_COOKIE)
    return {"logged_out": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "username": user.username}
