"""ORM models for ABPS. Mirrors the data model in the spec exactly."""
import datetime as dt

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
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
    lat = Column(Float, nullable=True)  # real coordinates from the source GeoJSON, when present
    lon = Column(Float, nullable=True)


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
    """Doubles as the asset master record: criticality is required by the
    rule-based scorer (Layer 3 baseline); asset_type/age_years/
    historical_failure_count/last_maintenance_date are the extra fields the
    ML risk model's feature engineering needs (§Layer 3A). Not every asset
    needs the ML fields populated — they default to neutral values so the
    rule-based scorer keeps working unchanged for assets that only ever
    register a criticality."""

    __tablename__ = "asset_criticality"

    asset_id = Column(String, primary_key=True)
    criticality = Column(Integer, nullable=False)  # 1-5
    asset_type = Column(String, default="")  # e.g. rail, ohe_insulator, signal_relay, point_machine
    age_years = Column(Float, default=0.0)
    historical_failure_count = Column(Integer, default=0)
    last_maintenance_date = Column(DateTime, nullable=True)


class ScoringConfig(Base):
    __tablename__ = "scoring_config"

    key = Column(String, primary_key=True)
    value = Column(Float, nullable=False)


class AppSetting(Base):
    """Small generic key/value store for non-numeric config that doesn't fit
    ScoringConfig's float-only value column — currently just the scoring
    source toggle (rule/ml/blend, see priority_engine.py)."""

    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, default="")


# ============================================================ LAYER 2: pipeline

class IngestionBatch(Base):
    """One record per bulk-import run (CSV/Excel or, in future, a live
    adapter pull) — the pipeline run stats the SRS asks to expose."""

    __tablename__ = "ingestion_batches"

    batch_id = Column(String, primary_key=True)
    source_system = Column(String, nullable=False)  # TMS|SMMS|TDMS|CSV|MANUAL
    department = Column(String, default="")
    filename = Column(String, default="")
    rows_in = Column(Integer, default=0)
    rows_valid = Column(Integer, default=0)
    rows_rejected = Column(Integer, default=0)
    rows_review = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)
    created_by = Column(String, default="")


class AssetIdMapping(Base):
    """Resolves a source system's own asset-ID convention to one canonical
    asset_id. TMS/SMMS/TDMS each format asset IDs differently in the real
    world (e.g. a bare track-chainage code vs. a prefixed asset tag) — this
    table is the explicit, inspectable translation layer between them."""

    __tablename__ = "asset_id_mappings"
    __table_args__ = (PrimaryKeyConstraint("source_system", "source_asset_id"),)

    source_system = Column(String, nullable=False)
    source_asset_id = Column(String, nullable=False)
    canonical_asset_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=now)


class CorridorIdMapping(Base):
    """Same idea as AssetIdMapping, for corridor identifiers — a source
    system's own corridor code resolved to the canonical {STATION}-{STATION}
    corridor_id derived from the real timetable."""

    __tablename__ = "corridor_id_mappings"
    __table_args__ = (PrimaryKeyConstraint("source_system", "source_corridor_id"),)

    source_system = Column(String, nullable=False)
    source_corridor_id = Column(String, nullable=False)
    canonical_corridor_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=now)


class ReviewQueueItem(Base):
    """A row the pipeline could not resolve (no known asset/corridor mapping,
    or a validation failure judged non-fatal) — held for human review rather
    than silently dropped or silently guessed at."""

    __tablename__ = "review_queue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String, default="")
    source_system = Column(String, default="")
    raw_row_json = Column(Text, default="{}")
    reason = Column(Text, default="")
    status = Column(String, default="pending")  # pending|resolved|discarded
    created_at = Column(DateTime, default=now)
    resolved_by = Column(String, default="")
    resolved_at = Column(DateTime, nullable=True)


# ============================================================ LAYER 3: ML

class MLModelRun(Base):
    """One record per model training run — the evaluation report data the
    SRS asks for (metrics, feature importances) lives here, not just in a
    console log, so it survives a restart and is queryable."""

    __tablename__ = "ml_model_runs"

    model_id = Column(String, primary_key=True)
    model_type = Column(String, nullable=False)  # risk_xgboost|risk_random_forest|traffic_sarima
    trained_at = Column(DateTime, default=now)
    trained_on = Column(String, default="synthetic")  # always "synthetic" today — see README
    metrics_json = Column(Text, default="{}")
    feature_importance_json = Column(Text, default="{}")
    notes = Column(Text, default="")


class TaskPrediction(Base):
    """The latest ML prediction for a task (overwritten on rescoring, one
    row per task) — kept separate from MaintenanceTask.priority_score/reason
    so the rule-based score is never overwritten by a model output; the two
    are compared explicitly rather than one silently replacing the other."""

    __tablename__ = "task_predictions"

    task_id = Column(String, primary_key=True)
    model_id = Column(String, default="")
    failure_risk_probability = Column(Float, default=0.0)
    urgency_score = Column(Float, default=0.0)
    criticality_score = Column(Float, default=0.0)
    ml_priority_score = Column(Float, default=0.0)
    shap_explanation = Column(Text, default="")
    predicted_at = Column(DateTime, default=now)
