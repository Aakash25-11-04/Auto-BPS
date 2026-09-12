"""Append-only audit trail helper."""
import json

from sqlalchemy.orm import Session

import models


def log(db: Session, action: str, user_id: str = "system", details: dict = None):
    entry = models.AuditLog(action=action, user_id=user_id or "system", details=json.dumps(details or {}))
    db.add(entry)
    db.commit()
    return entry
