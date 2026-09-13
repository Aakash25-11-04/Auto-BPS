"""Pydantic request/response schemas.

Datetime fields use one of two Annotated aliases (see tz_utils.py for the
full storage/display contract this codebase follows):
  UtcOut — RESPONSE fields. Attaches UTC tzinfo before Pydantic serializes,
    so the JSON value always carries an explicit offset (e.g. "...+00:00"),
    never a bare, ambiguous string.
  IstIn — REQUEST fields a user types a time into (corridor availability,
    freight forecast windows, manual reschedule). A naive submitted value
    (no offset — exactly what an <input type=datetime-local> sends) is
    interpreted as IST and converted to UTC; an explicit-offset value is
    honored as given.
"""
import datetime as dt
from typing import Annotated, List, Optional

from pydantic import BaseModel, BeforeValidator, Field

from tz_utils import ensure_utc_out, parse_user_local_datetime

UtcOut = Annotated[dt.datetime, BeforeValidator(ensure_utc_out)]
IstIn = Annotated[dt.datetime, BeforeValidator(parse_user_local_datetime)]


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
    created_at: UtcOut

    class Config:
        from_attributes = True


class MutualExclusionRequest(BaseModel):
    other_task_id: str


class CorridorAvailabilityCreate(BaseModel):
    corridor_id: str
    start_time: IstIn
    end_time: IstIn
    horizon: str = "weekly"


class DeriveAvailabilityRequest(BaseModel):
    corridor_id: str
    horizon: str = "weekly"  # weekly|monthly
    min_gap_hours: float = 1.5
    start_date: Optional[dt.date] = None


class FreightForecastCreate(BaseModel):
    corridor_id: str
    forecast_window_start: IstIn
    forecast_window_end: IstIn
    expected_goods_traffic: str = "medium"


class RescheduleRequest(BaseModel):
    new_start: IstIn


class ManualAssignRequest(BaseModel):
    """FR-COA-03 manual override: place an UNSCHEDULED (or not-yet-in-this-
    plan) task into a specific corridor window, bypassing the optimizer.
    NOTE: no user_id field, same reasoning as ApprovalRequest below — who
    performed the override is always the authenticated caller, never a
    client-supplied value."""

    plan_id: str
    task_id: str
    slot_id: str
    start_time_ist: IstIn
    reason: str = Field(min_length=1)


class ManualUnscheduleRequest(BaseModel):
    plan_id: str
    task_id: str
    reason: str = Field(min_length=1)


class ManualSwapRequest(BaseModel):
    plan_id: str
    incoming_task_id: str
    outgoing_task_id: str
    slot_id: str
    start_time_ist: IstIn
    reason: str = Field(min_length=1)


class OverrideRequestCreate(BaseModel):
    task_id: str
    plan_id: Optional[str] = ""
    reason: str = Field(min_length=1)


class OverrideRequestDecision(BaseModel):
    decision: str  # accept|decline
    decision_reason: Optional[str] = ""


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
    # Optional Layer-3 ML feature fields — left unset, an asset keeps whatever
    # it already had (or the neutral defaults in ml/features.py).
    asset_type: Optional[str] = None
    age_years: Optional[float] = None
    historical_failure_count: Optional[int] = None


class ScoringSourceUpdate(BaseModel):
    source: str  # rule | ml | blend


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
    expires_at: UtcOut
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


class WeatherIngestRequest(BaseModel):
    corridor_id: str
    days: int = Field(ge=1, le=16, default=7)


class WeatherRuleUpdate(BaseModel):
    rule_id: str
    label: Optional[str] = ""
    defect_type_pattern: str
    hazard: str  # lightning|wind|heat|rain|fog
    mode: str  # hard|soft|priority
    threshold: float = 0.0
    duration_buffer_pct: float = 0.0
    priority_points: float = 0.0
    active: bool = True


class DepartmentCapacityCreate(BaseModel):
    department: str  # ENG | TD | SNT
    date: Optional[dt.date] = None  # IST calendar date; omit for the department default
    max_concurrent_gangs: int = Field(ge=0, le=100)
    notes: Optional[str] = ""


class DepartmentCapacityUpdate(BaseModel):
    date: Optional[dt.date] = None
    max_concurrent_gangs: Optional[int] = Field(default=None, ge=0, le=100)
    notes: Optional[str] = None
