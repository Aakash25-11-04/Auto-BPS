"""Single source of truth for timezone handling across ABPS.

ABPS is an Indian Railways system — every timestamp a human sees must be
IST (UTC+5:30). IST has NO daylight saving — it is a fixed, constant offset
year-round, so converting to/from it is a simple, exact shift, never a
calendar-dependent lookup.

STORAGE CONTRACT: every datetime this application writes to the database
holds a UTC instant. This is enforced at the CONTRACT level, not the Python
type level: SQLite (this project's default) silently strips tzinfo from any
DateTime column on write regardless of whether the column is declared
DateTime(timezone=True) — confirmed by direct round-trip testing — so a
timezone-AWARE Python datetime would come back naive on the very next read
either way, and mixing aware/naive datetimes in application-internal
comparisons (e.g. "does this candidate window overlap this real train
occurrence") raises TypeError. Rather than fight the driver, every naive
datetime that flows through internal application code (utc_now(),
corridor-occurrence expansion, horizon-boundary math, everything compared
against a value read from the DB) is, BY UNIFORM CONTRACT, always UTC. The
only place a REAL tzinfo-aware datetime is ever constructed is at the read/
display boundary — ensure_utc()/to_ist() — immediately before a value is
serialized into an API response or otherwise shown to a human. That is
also where the "avoid silent ambiguity" goal actually lives: every API
response carries an explicit UTC offset (see routers' JSON encoding), so no
consumer of this API can ever misinterpret a timestamp's timezone.

API RESPONSE CONVENTION (documented per the project's own stated
principle of stating tradeoffs explicitly rather than leaving them
implicit): every API response returns UTC ISO-8601 WITH AN EXPLICIT
OFFSET (e.g. "2026-09-12T13:32:55+00:00") — never a bare, ambiguous
string. The FRONTEND converts to IST for display (frontend/index.html's
formatIST()/toIST()). This is architecturally the more correct choice
(keeps the backend timezone-agnostic; a future non-Indian deployment or
integration needs zero backend changes) and is applied uniformly: FastAPI's
datetime encoder is patched once, globally (see main.py), so this holds for
every endpoint without needing to touch each response individually.
"""
import datetime as dt

UTC = dt.timezone.utc
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")
IST_OFFSET = dt.timedelta(hours=5, minutes=30)


def utc_now() -> dt.datetime:
    """The one function every write path in this codebase uses instead of
    the ambiguous/deprecated datetime.utcnow() or the outright dangerous
    (server-local) datetime.now(). Naive, but a UTC value by the contract
    above — see module docstring for why naive is the deliberate choice
    here, not an oversight."""
    return dt.datetime.now(UTC).replace(tzinfo=None)


def ensure_utc(value) -> dt.datetime:
    """Attaches UTC tzinfo to a naive datetime (the storage contract above),
    or converts an already-aware datetime to UTC. Idempotent. This is the
    ONE place naive-by-contract values become real aware datetimes — always
    call this before serializing or converting to another zone."""
    if value is None:
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_ist(value) -> dt.datetime:
    """Converts any datetime (naive-assumed-UTC, or already aware) to an
    IST-aware datetime — the one function every display path should use."""
    v = ensure_utc(value)
    return v.astimezone(IST) if v else None


def utc_iso(value) -> str:
    """UTC ISO-8601 string with an explicit '+00:00' offset — what every
    API response uses (see module docstring)."""
    v = ensure_utc(value)
    return v.isoformat() if v else None


def ist_iso(value) -> str:
    """IST ISO-8601 string, e.g. '2026-09-12T19:02:55.181620+05:30' — used
    only where a value is deliberately pre-converted server-side (the
    corridor-search feature's response shape); everywhere else the frontend
    converts utc_iso() output for display."""
    v = to_ist(value)
    return v.isoformat() if v else None


def ist_now() -> dt.datetime:
    """The current instant, as an IST-aware datetime."""
    return dt.datetime.now(UTC).astimezone(IST)


def ist_today() -> dt.date:
    """The current IST CALENDAR date. NEVER use dt.date.today() anywhere in
    this codebase for a 'today'/horizon-start/'this week' computation —
    dt.date.today() is the server OS's local date, which is server-
    deployment-dependent and can differ from the IST date near midnight in
    either direction. Every horizon/date-range computation in ABPS has
    IST-based business meaning ("this week" for an Indian railway corridor
    means the IST week) and must use this instead."""
    return ist_now().date()


def ist_date_to_utc_bounds(d: dt.date) -> tuple:
    """Returns (start_utc, end_utc) as NAIVE-but-UTC datetimes (matching
    this module's internal-comparison convention) spanning the given IST
    calendar day — e.g. for a 'get everything scheduled on this IST date'
    query. Because IST has no DST, end_utc = start_utc + 24h is always
    exactly correct — no timezone-database lookup needed for the add."""
    start_ist = dt.datetime.combine(d, dt.time.min, tzinfo=IST)
    start_utc = start_ist.astimezone(UTC).replace(tzinfo=None)
    end_utc = start_utc + dt.timedelta(days=1)
    return start_utc, end_utc


def parse_user_local_datetime(value) -> dt.datetime:
    """Interprets a datetime submitted by a user — e.g. an <input
    type=datetime-local> value, or any ISO string with no offset — as IST,
    this system's one business timezone, and returns the equivalent
    naive-but-UTC datetime for storage/comparison. An input that DOES carry
    an explicit offset is honored as given and simply converted to UTC,
    never reinterpreted as IST. Used as a Pydantic BeforeValidator on every
    request schema field that accepts a user-typed time (corridor
    availability windows, freight forecast windows, manual reschedule) so
    'what a user types as 22:00 is interpreted as 22:00 IST' holds
    everywhere without each router repeating this conversion."""
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return value.astimezone(UTC).replace(tzinfo=None)


def ensure_utc_out(value) -> dt.datetime:
    """Pydantic BeforeValidator for RESPONSE schema datetime fields: attaches
    UTC tzinfo (see ensure_utc) so Pydantic's own JSON serialization emits
    an explicit offset — response_model routes bypass FastAPI's plain-dict
    JSON encoder patch (see main.py), so this is the equivalent fix for
    schema-typed responses specifically."""
    return ensure_utc(value)


def install_global_json_encoder() -> None:
    """Most ABPS routes return a plain dict built by hand (not a Pydantic
    response_model) — e.g. every route in routers/schedule.py, admin.py,
    corridor.py, decision.py, pipeline.py, weather.py. FastAPI serializes
    those through fastapi.encoders.jsonable_encoder, which by default calls
    a naive datetime's bare .isoformat() (no offset — exactly the ambiguous
    output this whole fix exists to eliminate). Registering utc_iso() as the
    encoder for datetime.datetime here is a single, global point of
    enforcement: every dict-returning route in the app emits an explicit
    UTC offset with no per-route change needed, verified by direct
    request/response testing (see README §9f). Pydantic response_model
    routes (schemas.TaskOut, TokenResponse) do NOT go through this path —
    those use the ensure_utc_out BeforeValidator in schemas.py instead,
    since Pydantic v2's own serializer bypasses fastapi.encoders entirely."""
    import fastapi.encoders as encoders

    encoders.ENCODERS_BY_TYPE[dt.datetime] = utc_iso
