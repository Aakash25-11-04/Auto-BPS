"""Real authentication and authorization for ABPS.

Replaces the old "trust whatever X-User-Role header the client sends"
scheme entirely. Every write and every piece of task-level data now flows
through the dependencies defined here — there are no inline `if role == ...`
checks scattered in route bodies, because that is exactly where holes appear
(NFR-04). See routers/auth.py for the login/logout/refresh/me endpoints that
issue and consume the tokens this module verifies.

Design choices, stated explicitly rather than left implicit:
  - Password hashing uses the `bcrypt` package directly, not passlib.
    passlib 1.7.4 (the last release) has a known incompatibility with
    bcrypt>=4.1's version-detection shim (AttributeError on
    `_bcrypt.__about__`) that made it fail outright in this environment.
    bcrypt itself is actively maintained and this avoids a broken
    abstraction layer for no benefit.
  - JWT (python-jose) rather than server-side sessions, because the app
    already has no session store and adding one just to hold sessions would
    be more moving parts than a signed, stateless token for a single-process
    demo deployment.
  - Two token types: a short-lived ACCESS token (used on every request) and
    a longer-lived REFRESH token (used only to mint a new access token).
    Logout/rotation revokes a refresh token's `jti` in the RevokedToken
    table. Access tokens are NOT checked against that table — revoking them
    would mean a DB lookup on every single authenticated request; instead
    they simply expire quickly. This is a standard, documented tradeoff.
  - Account lockout (not IP-based rate limiting) is the concrete
    "rate-limit login attempts" mechanism implemented here: N consecutive
    failed passwords locks that specific account for a cooldown window.
    True IP-level throttling would need an additional layer (e.g. slowapi +
    a shared store) which is out of scope for a single-process app with no
    reverse proxy in front of it here — noted as a known gap, not silently
    skipped.
"""
import datetime as dt
import os
import secrets
import uuid

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

import models
from database import get_db
from tz_utils import utc_now

# --- configuration -----------------------------------------------------

SECRET_KEY = os.environ.get("ABPS_SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(48)
    print(
        "\n*** WARNING: ABPS_SECRET_KEY is not set. Using a random key generated for this "
        "process only — every issued token will become invalid the next time the server "
        "restarts, and this is NOT safe for a real deployment. Set ABPS_SECRET_KEY to a "
        "fixed, secret value before deploying. ***\n"
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 20
REFRESH_TOKEN_EXPIRE_DAYS = 7

MAX_FAILED_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

_bearer_scheme = HTTPBearer(auto_error=False)


# --- password hashing ----------------------------------------------------

def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False  # malformed hash


# --- token issuance --------------------------------------------------------

def _new_token(user: models.User, token_type: str, expires_delta: dt.timedelta, extra_claims: dict = None) -> tuple:
    now = utc_now()
    jti = uuid.uuid4().hex
    exp = now + expires_delta
    payload = {
        "sub": user.user_id,
        "role": user.role,
        "department": user.department or "",
        "type": token_type,
        "jti": jti,
        "iat": now,
        "exp": exp,
    }
    if extra_claims:
        payload.update(extra_claims)
    # jwt.encode() mutates `payload` in place, converting datetime claims
    # (exp/iat) to raw POSIX-timestamp ints for the wire format — reading
    # payload["exp"] back out AFTER this call silently returns an int, not
    # the datetime the docstring/callers expect. This was a real latent bug
    # (papered over by Pydantic's implicit int->datetime coercion on the
    # old plain `dt.datetime` schema field) that the stricter UtcOut
    # validator (see schemas.py) surfaces. Returning the `exp` captured
    # BEFORE encoding is the actual fix.
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token, jti, exp


def create_access_token(user: models.User, extra_claims: dict = None) -> tuple:
    """Returns (token, expires_at)."""
    token, _jti, exp = _new_token(user, "access", dt.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES), extra_claims)
    return token, exp


def create_refresh_token(user: models.User) -> tuple:
    """Returns (token, jti, expires_at)."""
    return _new_token(user, "refresh", dt.timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))


def decode_token(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if payload.get("type") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"expected a {expected_type} token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def decode_refresh_token(token: str, db: Session) -> dict:
    payload = decode_token(token, "refresh")
    if db.query(models.RevokedToken).filter_by(jti=payload["jti"]).first():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token has been revoked")
    return payload


def revoke_refresh_token(db: Session, payload: dict):
    if db.query(models.RevokedToken).filter_by(jti=payload["jti"]).first():
        return
    db.add(models.RevokedToken(jti=payload["jti"], expires_at=dt.datetime.fromtimestamp(payload["exp"], tz=dt.timezone.utc).replace(tzinfo=None)))
    db.commit()


# --- account lockout -------------------------------------------------------

def is_locked(user: models.User) -> bool:
    return bool(user.locked_until and user.locked_until > utc_now())


def register_failed_login(db: Session, user: models.User) -> bool:
    """Increments the failure counter and locks the account if the
    threshold is hit. Returns True if this call is what triggered the
    lockout (so the caller can audit-log it as a distinct event)."""
    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
    newly_locked = False
    if user.failed_login_attempts >= MAX_FAILED_LOGIN_ATTEMPTS:
        user.locked_until = utc_now() + dt.timedelta(minutes=LOCKOUT_MINUTES)
        newly_locked = True
    db.commit()
    return newly_locked


def register_successful_login(db: Session, user: models.User):
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()


# --- FastAPI dependencies ---------------------------------------------------

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated — no bearer token supplied",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials, "access")
    user = db.query(models.User).filter_by(user_id=payload.get("sub")).first()
    if not user or not user.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_role(*allowed_roles: str):
    """Reusable dependency factory — the ONLY place role checks happen.
    Usage: Depends(require_role("COA")) or Depends(require_role("ADMIN", "COA"))."""

    def _dependency(user: models.User = Depends(get_current_user)) -> models.User:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{user.role}' is not permitted to call this endpoint (requires {sorted(allowed_roles)})",
            )
        return user

    return _dependency
