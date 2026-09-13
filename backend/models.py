"""ORM models for ABPS. Mirrors the data model in the spec exactly."""
import datetime as dt

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)

from database import Base
from tz_utils import utc_now


def now():
    # Every default=now/onupdate=now column below (created_at, updated_at,
    # timestamp, fetched_at, etc. — ~15 columns across this file) is fixed
    # by this one function: utc_now() is the non-ambiguous, non-deprecated
    # replacement for datetime.utcnow(). See tz_utils.py for why this is
    # naive-but-UTC by deliberate contract, not an oversight.
    return utc_now()


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
    # submitted|scheduled|unscheduled|cancelled|manually_removed|displaced
    # manually_removed: a COA/ADMIN deliberately pulled this task out of the
    # plan (see scheduler.manual_unschedule_task) — distinct from
    # "unscheduled" (the optimizer tried and had no room) and "displaced"
    # (bumped out by a manual-swap to make room for another task).
    status = Column(String, default="submitted")
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
    derived_from = Column(String, default="timetable_gap")  # timetable_gap|manual|ml_forecast
    horizon = Column(String, default="weekly")  # weekly|monthly
    created_at = Column(DateTime, default=now)
    # Vacancy provenance (see vacancy.py): which REAL trains bracket this
    # gap, and what safety buffer was applied when computing it. These make
    # a computed window independently verifiable against the timetable
    # ("after train X, before train Y") rather than something a user has to
    # take on trust.
    train_before_id = Column(String, default="")
    train_after_id = Column(String, default="")
    buffer_minutes = Column(Integer, default=0)
    sections_considered = Column(Integer, default=1)  # how many sections had to be simultaneously free


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
    """Station master. Loaded and MERGED from real open datasets (see
    station_loader.py) — never invented. station_type is derived from the
    real station name by a documented heuristic (the open sources carry no
    explicit type field) and is what corridor derivation uses to pick
    natural corridor endpoints (junctions/terminals)."""

    __tablename__ = "stations"

    station_code = Column(String, primary_key=True)
    station_name = Column(String, nullable=False)
    station_name_hindi = Column(String, default="")
    state = Column(String, default="")
    zone = Column(String, default="")
    division = Column(String, default="")
    address = Column(String, default="")
    lat = Column(Float, nullable=True)  # real coordinates from the source GeoJSON, when present
    lon = Column(Float, nullable=True)
    elevation_m = Column(Float, nullable=True)
    station_type = Column(String, default="regular")  # junction|terminal|halt|cabin|regular
    source = Column(String, default="")  # which dataset this row's fields came from
    updated_at = Column(DateTime, default=now, onupdate=now)


class Corridor(Base):
    """A REAL corridor, derived from actual railway geography (see
    corridor_builder.py) — never invented.

    kind='section'  : one physical track section between two ADJACENT
                      stations, exactly as the real timetable's consecutive
                      stops define it. This is the atomic unit everything
                      else is built from, and matches the corridor_id
                      convention already used across the system.
    kind='route'    : a contiguous CHAIN of sections between two
                      significant stations (junction/terminal endpoints —
                      how railway staff actually refer to a stretch of
                      line), carrying its ordered intermediate stations,
                      real polyline geometry from station coordinates, and
                      distance.

    Both kinds live in one table because both are genuinely corridors and
    the scheduler/vacancy logic treats them identically — a section is just
    a route with one section, so nothing downstream needs to special-case
    them."""

    __tablename__ = "corridors"

    corridor_id = Column(String, primary_key=True)  # "{FROM}-{TO}", e.g. NDLS-GZB
    kind = Column(String, default="section")  # section|route
    from_station_code = Column(String, nullable=False)
    to_station_code = Column(String, nullable=False)
    intermediate_stations = Column(Text, default="[]")  # ordered JSON array of station codes
    section_ids = Column(Text, default="[]")  # ordered JSON array of section corridor_ids
    section_count = Column(Integer, default=1)
    zone = Column(String, default="")
    total_distance_km = Column(Float, nullable=True)
    geometry_json = Column(Text, default="")  # GeoJSON LineString from real station coords
    train_count = Column(Integer, default=0)  # distinct real trains observed traversing it
    derived_from = Column(String, default="timetable")
    created_at = Column(DateTime, default=now)


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
    # Set the moment a manual override (assign/unschedule/swap) is applied to
    # a plan that is already 'published' — the override itself is the COA's
    # decision and does NOT require re-approval, but a published plan that
    # has since been hand-adjusted must stay visibly distinguishable from one
    # that is exactly what the optimizer/approval produced.
    modified_after_publication = Column(Boolean, default=False)


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
    # Manual override provenance (FR-COA-03): set by
    # scheduler.manual_assign_task/manual_swap_tasks whenever a COA/ADMIN
    # placed this entry directly rather than the optimizer. Carried forward
    # verbatim across a scheduler re-run when the task is pinned (see
    # scheduler._apply_manual_pins) so the visual "manually placed" marker
    # and audit trail survive re-optimization, not just the one run that
    # created it.
    override = Column(Boolean, default=False)
    override_reason = Column(Text, default="")
    override_by = Column(String, default="")
    override_at = Column(DateTime, nullable=True)


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
    created_at = Column(DateTime, default=now)


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


class OverrideRequest(Base):
    """A department's REQUEST that the Control Office manually schedule
    their (currently unscheduled) task, or otherwise intervene ahead of the
    optimizer's own placement — the request half of FR-COA-03's manual
    override. A department can only ask; only COA/ADMIN can actually
    execute an override via manual_assign_task/manual_unschedule_task/
    manual_swap_tasks in scheduler.py. Deciding a request (accept/decline)
    does not by itself schedule anything — it just records the Control
    Office's answer; a COA who accepts one is still expected to place the
    task using the ordinary manual-assign/-swap endpoints (which, if given
    the same task_id, auto-close the matching pending request)."""

    __tablename__ = "override_requests"

    request_id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False)
    plan_id = Column(String, default="")
    department = Column(String, default="")
    requested_by = Column(String, default="")
    reason = Column(Text, default="")
    status = Column(String, default="pending")  # pending|accepted|declined
    decision_reason = Column(Text, default="")
    decided_by = Column(String, default="")
    decided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now)


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


# ============================================================ WEATHER (7th data source)

class WeatherForecastEntry(Base):
    """One day's forecast for one corridor, resolved to the corridor's
    nearest real station (see weather_service.nearest_station_for_corridor).
    source is 'real' (fetched live from the configured WeatherSourceAdapter,
    e.g. Open-Meteo) or 'synthetic' (clearly-labelled fallback used only when
    no live adapter is reachable — never silently presented as real, exactly
    like the existing demo-task/ML-training-data provenance convention)."""

    __tablename__ = "weather_forecast_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    corridor_id = Column(String, nullable=False)
    station_code = Column(String, default="")  # the real station this forecast was resolved against
    forecast_date = Column(DateTime, nullable=False)  # midnight UTC of the date this forecast is FOR
    precipitation_probability_pct = Column(Float, nullable=False)  # 0-100
    wind_speed_kmh = Column(Float, nullable=False)  # >= 0, daily max
    visibility_km = Column(Float, default=10.0)  # mean visibility; low value => fog
    fog_risk = Column(Boolean, default=False)  # derived: visibility_km below the configured fog threshold
    lightning_risk = Column(Boolean, default=False)  # derived from provider weather code / thunderstorm flag
    temperature_max_c = Column(Float, nullable=False)
    temperature_min_c = Column(Float, nullable=False)
    source = Column(String, default="synthetic")  # real|synthetic — provenance, never silently blended
    provider = Column(String, default="")  # e.g. "open-meteo", "synthetic_fallback"
    fetched_at = Column(DateTime, default=now)


class WeatherHourlyForecast(Base):
    """HOURLY forecast from Open-Meteo for a corridor's real coordinates.

    Hourly (not daily) is what makes weather genuinely usable for block
    scheduling: a maintenance window is a few hours, and "thunderstorm
    somewhere today" is not the same statement as "thunderstorm 04:00-05:00
    IST". Verified live on this build: Delhi 2026-09-12 showed WMO code 95
    (thunderstorm) at exactly 04:00 and 05:00 IST while the rest of the day
    was clear — a daily roll-up would have excluded the entire day.

    WeatherForecastEntry (the daily table) is still maintained as a roll-up
    of these rows, so every existing daily-granularity consumer (the
    admin-tunable rule table, timeline overlay, priority scoring) keeps
    working unchanged while the optimizer's per-window check uses these
    precise hourly rows."""

    __tablename__ = "weather_hourly_forecasts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    corridor_id = Column(String, nullable=False)
    valid_time = Column(DateTime, nullable=False)  # start of the hour, naive-but-UTC (see tz_utils)
    temperature_c = Column(Float, nullable=True)
    precipitation_probability_pct = Column(Float, nullable=True)
    rain_mm = Column(Float, nullable=True)
    wind_speed_kmh = Column(Float, nullable=True)
    visibility_km = Column(Float, nullable=True)
    weather_code = Column(Integer, nullable=True)  # WMO code; 95/96/99 = thunderstorm
    lightning_risk = Column(Boolean, default=False)
    fog_risk = Column(Boolean, default=False)
    source = Column(String, default="open_meteo")
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    fetched_at = Column(DateTime, default=now)


class WeatherSensitivityRule(Base):
    """Admin-tunable table driving all three weather behaviors (hard
    exclusion, soft duration buffering, priority scoring) — a single,
    inspectable, editable-by-Administrator surface rather than logic buried
    in the scheduler or priority engine (mirrors ScoringConfig's role for the
    rule-based priority weights).

    defect_type_pattern: lower-cased substring matched against
        MaintenanceTask.defect_type ("*" matches every defect type).
    hazard: lightning | wind | heat | rain | fog.
    mode:
        hard     — safety-critical CP-SAT candidate exclusion. threshold is
                   ignored for hazard='lightning' (any forecast lightning
                   risk excludes); for wind/heat, threshold is the kmh/°C
                   value forecast must exceed to exclude.
        soft     — inflates the task's effective required duration used by
                   the solver by duration_buffer_pct when the forecast
                   crosses `threshold` (rain: precipitation_probability_pct;
                   fog: visibility_km BELOW threshold).
        priority — adds priority_points (scaled by priority_engine's
                   w_weather_risk weight) to a matching task's score when the
                   forecast crosses `threshold` within the reliable-forecast
                   horizon.
    """

    __tablename__ = "weather_sensitivity_rules"

    rule_id = Column(String, primary_key=True)
    label = Column(String, default="")
    defect_type_pattern = Column(String, nullable=False)
    hazard = Column(String, nullable=False)  # lightning|wind|heat|rain|fog
    mode = Column(String, nullable=False)  # hard|soft|priority
    threshold = Column(Float, default=0.0)
    duration_buffer_pct = Column(Float, default=0.0)  # soft mode
    priority_points = Column(Float, default=0.0)  # priority mode
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


# ============================================================ CREW & RESOURCE CAPACITY (Feature 10)

class DepartmentCapacity(Base):
    """How many independent work parties ("gangs") a department can field
    SIMULTANEOUSLY across ALL corridors — a department-wide resource, not a
    per-corridor one. date=None is the standing default for that department;
    a row with a date is an override for exactly that IST calendar day
    (00:00-24:00 IST), e.g. "SNT has only 2 gangs on 15 Sep due to
    training". Enforced as a HARD constraint by the optimizer and by every
    manual-override path — see crew_capacity.py."""

    __tablename__ = "department_capacity"

    id = Column(Integer, primary_key=True, autoincrement=True)
    department = Column(String, nullable=False)  # ENG | TD | SNT
    date = Column(Date, nullable=True)  # IST calendar date, or NULL for the department default
    max_concurrent_gangs = Column(Integer, nullable=False)
    notes = Column(Text, default="")
    updated_by = Column(String, default="")
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class GangAssignment(Base):
    """Which gang executes which scheduled task in a plan. Populated
    POST-SOLVE by a greedy interval-colouring pass (crew_capacity.assign_gangs)
    — the CP-SAT model only enforces the concurrent-gang COUNT; naming the
    gangs is a labelling step afterwards. Rebuilt whenever a plan's entries
    change (scheduler run or manual override), so it always matches the
    plan's current entries."""

    __tablename__ = "gang_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(String, nullable=False, index=True)
    task_id = Column(String, nullable=False)
    department = Column(String, default="")
    gang_id = Column(String, nullable=False)  # e.g. "ENG-GANG-1"
    corridor_id = Column(String, default="")
    assigned_window_start = Column(DateTime, nullable=False)
    assigned_window_end = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=now)


class TaskWeatherAssessment(Base):
    """Audit record of a weather-driven scheduling decision for one task —
    what forecast was used, which rule fired, and (for a soft buffer) both
    the base and weather-adjusted duration the solver actually used. Written
    at plan-materialization time so every weather-influenced outcome is
    inspectable after the fact, not just implied by the final placement."""

    __tablename__ = "task_weather_assessments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, nullable=False)
    plan_id = Column(String, default="")
    horizon = Column(String, default="weekly")
    corridor_id = Column(String, default="")
    slot_id = Column(String, default="")
    outcome = Column(String, default="")  # hard_excluded|soft_buffered|clear
    base_duration_hours = Column(Float, default=0.0)
    effective_duration_hours = Column(Float, default=0.0)
    buffer_pct_applied = Column(Float, default=0.0)
    hazard = Column(String, default="")
    reason = Column(Text, default="")
    forecast_precipitation_pct = Column(Float, nullable=True)
    forecast_wind_kmh = Column(Float, nullable=True)
    forecast_temp_max_c = Column(Float, nullable=True)
    forecast_lightning_risk = Column(Boolean, nullable=True)
    forecast_source = Column(String, default="")
    created_at = Column(DateTime, default=now)
