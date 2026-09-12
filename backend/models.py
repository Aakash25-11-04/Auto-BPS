"""ORM models for ABPS. Mirrors the data model in the spec exactly."""
import datetime as dt

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from database import Base


def now():
    return dt.datetime.utcnow()


class MaintenanceTask(Base):
    __tablename__ = "maintenance_tasks"

    task_id = Column(String, primary_key=True)
    department = Column(String, nullable=False)  # ENG | TD | SNT
    asset_id = Column(String, nullable=False)
    defect_type = Column(String, nullable=False)
    severity = Column(Integer, nullable=False)  # 1-5
    overdue_days = Column(Integer, nullable=False, default=0)
    required_duration_hours = Column(Float, nullable=False)
    corridor_id = Column(String, nullable=False)
    safety_critical = Column(Boolean, default=False)
    interlocking_critical = Column(Boolean, default=False)
    mutually_exclusive_with = Column(Text, default="")  # comma-separated task_ids
    priority_score = Column(Float, default=0.0)
    priority_reason = Column(Text, default="")
    status = Column(String, default="submitted")  # submitted|scheduled|unscheduled|cancelled
    unscheduled_reason = Column(Text, default="")
    source = Column(String, default="manual")  # manual|csv|adapter|demo
    source_ref = Column(String, default="")
    created_by = Column(String, default="")
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class CorridorSlot(Base):
    __tablename__ = "corridor_slots"

    slot_id = Column(String, primary_key=True)
    corridor_id = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    status = Column(String, default="available")  # available|used|cancelled
    derived_from = Column(String, default="timetable_gap")  # timetable_gap|manual
    horizon = Column(String, default="weekly")  # weekly|monthly
    created_at = Column(DateTime, default=now)


class TrainTimetableEntry(Base):
    __tablename__ = "train_timetable_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    train_id = Column(String, nullable=False)
    train_name = Column(String, default="")
    from_station_code = Column(String, nullable=False)
    to_station_code = Column(String, nullable=False)
    corridor_id = Column(String, nullable=False)
    scheduled_departure = Column(DateTime, nullable=False)
    scheduled_arrival = Column(DateTime, nullable=False)
    service_type = Column(String, default="")
    source = Column(String, default="datameet_github_mirror")


class Station(Base):
    __tablename__ = "stations"

    station_code = Column(String, primary_key=True)
    station_name = Column(String, nullable=False)
    zone = Column(String, default="")


class FreightForecastEntry(Base):
    __tablename__ = "freight_forecast_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    corridor_id = Column(String, nullable=False)
    forecast_window_start = Column(DateTime, nullable=False)
    forecast_window_end = Column(DateTime, nullable=False)
    expected_goods_traffic = Column(String, default="medium")  # low|medium|high
    source = Column(String, default="manual")


class BlockPlan(Base):
    __tablename__ = "block_plans"

    plan_id = Column(String, primary_key=True)
    horizon = Column(String, nullable=False)  # weekly|monthly
    version = Column(Integer, nullable=False)
    status = Column(String, default="draft")  # draft|published|rejected|superseded
    metrics_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=now)
    approved_by = Column(String, default="")
    approved_at = Column(DateTime, nullable=True)


class BlockPlanEntry(Base):
    __tablename__ = "block_plan_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(String, ForeignKey("block_plans.plan_id"), nullable=False)
    task_id = Column(String, nullable=False)
    slot_id = Column(String, nullable=False)
    corridor_id = Column(String, default="")
    department = Column(String, default="")
    assigned_window_start = Column(DateTime, nullable=False)
    assigned_window_end = Column(DateTime, nullable=False)
    co_scheduled_departments = Column(String, default="")  # comma-separated


class User(Base):
    __tablename__ = "users"

    user_id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False)  # ENG|TD|SNT|COA|ADMIN
    department = Column(String, default="")
    active = Column(Boolean, default=True)
    password_hash = Column(String, nullable=True)  # bcrypt; NEVER plaintext (NFR-05)
    failed_login_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)  # account lockout expiry, or None if not locked


class RevokedToken(Base):
    """Refresh tokens are stateless JWTs, so logout/rotation needs an
    explicit revocation record — this table is that record. Access tokens
    are short-lived and NOT checked against this table (a deliberate,
    documented tradeoff: revoking them would require a lookup on every
    request; they simply expire quickly instead)."""

    __tablename__ = "revoked_tokens"

    jti = Column(String, primary_key=True)
    expires_at = Column(DateTime, nullable=False)  # for periodic cleanup; not enforced automatically
    revoked_at = Column(DateTime, default=now)


class AuditLog(Base):
    __tablename__ = "audit_log"

    log_id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String, nullable=False)
    user_id = Column(String, default="system")
    timestamp = Column(DateTime, default=now)
    details = Column(Text, default="")


class AssetCriticality(Base):
    __tablename__ = "asset_criticality"

    asset_id = Column(String, primary_key=True)
    criticality = Column(Integer, nullable=False)  # 1-5


class ScoringConfig(Base):
    __tablename__ = "scoring_config"

    key = Column(String, primary_key=True)
    value = Column(Float, nullable=False)
