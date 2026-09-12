"""Pydantic request/response schemas."""
import datetime as dt
from typing import List, Optional

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    department: str
    asset_id: str
    defect_type: str
    severity: int = Field(ge=1, le=5)
    overdue_days: int = Field(ge=0, default=0)
    required_duration_hours: float = Field(gt=0)
    corridor_id: str
    safety_critical: bool = False
    interlocking_critical: bool = False
    mutually_exclusive_with: Optional[List[str]] = None
    source: Optional[str] = "manual"
    source_ref: Optional[str] = ""
    # created_by is deliberately NOT a client-settable field anymore — it is
    # always taken from the authenticated user, never trusted from the body.


class TaskOut(BaseModel):
    task_id: str
    department: str
    asset_id: str
    defect_type: str
    severity: int
    overdue_days: int
    required_duration_hours: float
    corridor_id: str
    safety_critical: bool
    interlocking_critical: bool
    mutually_exclusive_with: str
    priority_score: float
    priority_reason: str
    status: str
    unscheduled_reason: str
    source: str
    source_ref: str
    created_by: str
    created_at: dt.datetime

    class Config:
        from_attributes = True


class MutualExclusionRequest(BaseModel):
    other_task_id: str


class CorridorAvailabilityCreate(BaseModel):
    corridor_id: str
    start_time: dt.datetime
    end_time: dt.datetime
    horizon: str = "weekly"


class DeriveAvailabilityRequest(BaseModel):
    corridor_id: str
    horizon: str = "weekly"  # weekly|monthly
    min_gap_hours: float = 1.5
    start_date: Optional[dt.date] = None


class FreightForecastCreate(BaseModel):
    corridor_id: str
    forecast_window_start: dt.datetime
    forecast_window_end: dt.datetime
    expected_goods_traffic: str = "medium"


class RescheduleRequest(BaseModel):
    new_start: dt.datetime


class ApprovalRequest(BaseModel):
    plan_id: str
    decision: str  # approve|reject
    # NOTE: no user_id field — who approved/rejected is always taken from the
    # authenticated caller's token, never from client-supplied input. Letting
    # a client claim to be "COA-1" regardless of who they actually are is
    # exactly the hole that made the old audit trail decorative.


class ScoringConfigUpdate(BaseModel):
    key: str
    value: float


class AssetCriticalityUpdate(BaseModel):
    asset_id: str
    criticality: int = Field(ge=1, le=5)


class UserCreate(BaseModel):
    user_id: str
    name: str
    role: str
    department: Optional[str] = ""
    active: bool = True
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_at: dt.datetime
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class ImpersonateRequest(BaseModel):
    target_user_id: str
