"""Authentication endpoints: login, logout, token refresh, identity, password
change, and admin-gated impersonation ("login as" for demos).

Every credential check and every audit entry here uses the REAL user —
there is no header the client can set to claim a different identity. The one
deliberate exception is /impersonate, which is itself gated behind
require_role("ADMIN") and writes an audit entry naming both the admin and
the account being impersonated, precisely so it can never be mistaken for
an unauthenticated bypass.
"""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import auth
import models
import schemas
from audit import log
from database import get_db
from tz_utils import ist_iso, utc_iso

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_public(user: models.User) -> dict:
    return {"user_id": user.user_id, "name": user.name, "role": user.role, "department": user.department}


@router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(user_id=payload.username).first()

    # Constant-shape failure path: whether the username exists or not, we
    # still do a bcrypt verify against *something* so response timing doesn't
    # trivially reveal valid usernames. verify_password("", None) is cheap,
    # so this is a soft mitigation, not a full constant-time guarantee.
    if not user or not user.active:
        auth.verify_password(payload.password, auth.hash_password("dummy"))
        log(db, "login_failed", payload.username, {"reason": "unknown or inactive user"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    if auth.is_locked(user):
        # user.locked_until is naive-but-UTC (see tz_utils.py) — a bare
        # .isoformat() call on it would silently omit the offset (exactly
        # the ambiguous-timestamp bug this whole fix exists to eliminate);
        # both the audit detail and the message shown to the locked-out
        # user go through ist_iso() so what a human is told is IST, like
        # every other user-facing timestamp in this app.
        log(db, "login_blocked_locked", user.user_id, {"locked_until": utc_iso(user.locked_until)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Account locked until {ist_iso(user.locked_until)} IST after repeated failed attempts",
        )

    if not auth.verify_password(payload.password, user.password_hash):
        newly_locked = auth.register_failed_login(db, user)
        log(db, "login_failed", user.user_id, {"attempt_count": user.failed_login_attempts})
        if newly_locked:
            log(db, "account_locked", user.user_id, {"locked_until": utc_iso(user.locked_until), "reason": "too many failed attempts"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    auth.register_successful_login(db, user)
    access_token, expires_at = auth.create_access_token(user)
    refresh_token, _jti, _exp = auth.create_refresh_token(user)
    log(db, "login_success", user.user_id, {"role": user.role})

    return schemas.TokenResponse(
        access_token=access_token, refresh_token=refresh_token, expires_at=expires_at, user=_user_public(user)
    )


@router.post("/refresh", response_model=schemas.TokenResponse)
def refresh(payload: schemas.RefreshRequest, db: Session = Depends(get_db)):
    old_payload = auth.decode_refresh_token(payload.refresh_token, db)
    user = db.query(models.User).filter_by(user_id=old_payload["sub"]).first()
    if not user or not user.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or deactivated")

    # rotate: the old refresh token is single-use
    auth.revoke_refresh_token(db, old_payload)
    access_token, expires_at = auth.create_access_token(user)
    new_refresh_token, _jti, _exp = auth.create_refresh_token(user)

    return schemas.TokenResponse(
        access_token=access_token, refresh_token=new_refresh_token, expires_at=expires_at, user=_user_public(user)
    )


@router.post("/logout")
def logout(
    payload: schemas.LogoutRequest,
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    if payload.refresh_token:
        try:
            refresh_payload = auth.decode_refresh_token(payload.refresh_token, db)
            auth.revoke_refresh_token(db, refresh_payload)
        except HTTPException:
            pass  # already invalid/expired/revoked — logout is idempotent either way
    log(db, "logout", current_user.user_id, {})
    return {"detail": "logged out"}


@router.get("/me")
def me(current_user: models.User = Depends(auth.get_current_user)):
    return _user_public(current_user)


@router.post("/change-password")
def change_password(
    payload: schemas.ChangePasswordRequest,
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    if not auth.verify_password(payload.current_password, current_user.password_hash):
        log(db, "password_change_failed", current_user.user_id, {"reason": "current password incorrect"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="current password is incorrect")

    current_user.password_hash = auth.hash_password(payload.new_password)
    db.commit()
    log(db, "password_changed", current_user.user_id, {})
    return {"detail": "password changed"}


@router.post("/impersonate", response_model=schemas.TokenResponse)
def impersonate(
    payload: schemas.ImpersonateRequest,
    admin_user: models.User = Depends(auth.require_role("ADMIN")),
    db: Session = Depends(get_db),
):
    """Demo/support convenience: an authenticated ADMIN can obtain a working
    session as another user, WITHOUT that user's password, so the UI's role
    switcher keeps working for demos. This is not a bypass — it requires a
    real ADMIN session, issues an ACCESS-ONLY token (no refresh token, so the
    impersonated session expires quickly and can't be silently renewed), and
    is always audit-logged with both identities."""
    target = db.query(models.User).filter_by(user_id=payload.target_user_id).first()
    if not target or not target.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="target user not found or inactive")

    access_token, expires_at = auth.create_access_token(target, extra_claims={"impersonated_by": admin_user.user_id})
    log(db, "user_impersonation_started", admin_user.user_id, {"target_user_id": target.user_id, "target_role": target.role})

    return schemas.TokenResponse(access_token=access_token, refresh_token=None, expires_at=expires_at, user=_user_public(target))
