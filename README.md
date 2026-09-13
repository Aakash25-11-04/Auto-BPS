# ABPS — Automatic Block Planning System

An AI-assisted platform that coordinates maintenance corridor "block"
scheduling across three Indian Railways departments — Engineering (ENG),
Traction Distribution (TD), and Signal & Telecom (SNT) — around a Control
Office (COA) view of real train traffic. It scores every maintenance task by
urgency/risk (rule-based, ML, or a blend of both), then uses Google OR-Tools
CP-SAT to pack compatible tasks from *different* departments into the *same*
corridor closure wherever possible, so the corridor shuts once instead of
three times.

Nothing is ever scheduled outside human control: ML models only ever
estimate parameters (a risk probability, a forecasted low-traffic window) —
CP-SAT makes every actual scheduling decision under hard constraints, and it
only ever produces a **draft** plan. It becomes real only when the Control
Office explicitly approves it.

## Architecture — six layers

| Layer | What it does | Where |
|---|---|---|
| **1. Data Sources** | TMS/SMMS/TDMS/COA behind a pluggable adapter interface; real train timetable + freight forecast | `backend/adapters/`, §2 |
| **2. Data Integration & Preprocessing** | Ingestion → validation → cleaning → normalization → asset/corridor ID mapping → unified DB (SQLite or Postgres) | `backend/pipeline/`, §2b |
| **3. AI / Analytics** | XGBoost + Random Forest risk/priority prediction (SHAP-explained), SARIMA traffic forecasting — synthetic training data, honestly labelled | `backend/ml/`, §5b |
| **4. Scheduling & Optimization** | Candidate generation → multi-department clustering → CP-SAT interval scheduling under corridor, mutual-exclusion and **department gang (crew) capacity** constraints | `backend/scheduler.py`, `backend/crew_capacity.py`, §6, §11f |
| **5. Decision Intelligence** | Emergency re-optimization, what-if analysis, explainability/counterfactuals, shadow prices | `backend/decision_intelligence.py`, §6c |
| **6. UI & Decision Support** | Role-adaptive dashboard: priority queue, corridor timeline, KPI/before-after, crew utilization strip, live Leaflet railway network map | `frontend/index.html`, §8c, §11g |

Weather-aware scheduling (§9) is an **additive** capability layered across 1/2/3/4/6 above — a 7th data source, a scoring input, an optimizer feasibility check, and a timeline overlay — not a new layer of its own and not a rewrite of anything above.

Crew & resource capacity (§11f) and the live railway network map (§11g) are likewise additive, and independent of each other: the optimizer never imports map code, and the map degrades to "no gang labels" if the crew module is removed.

Governance is identical across all six layers and never optional: **ML never
schedules anything** (§5b/§7), **every optimizer run is a draft until COA
approves it** (§7), **a failed run never touches the last published plan**
(§7), and **every action — ingestion, training, scheduling, approval,
override — is audit-logged under the real acting user** (§7).

---

## 1. Quick start

### Requirements
- Python 3.10+ (tested on 3.14)
- ~150 MB free disk (the real train-timetable dataset) plus ~1GB for the
  ML libraries (xgboost/shap/scikit-learn/statsmodels) — all pure-Python
  wheels, no separate system install needed
- Internet access the first time you load the timetable (or pre-fetch the
  files per §2 below and run fully offline afterwards)
- Training a risk model (`POST /api/ml/train`) and everything else in §5b
  are optional — the app is fully usable with the rule-based scorer alone
- Docker is only needed if you want real PostgreSQL instead of the SQLite
  default — see §2b

### macOS / Linux / WSL / git-bash
```bash
export ABPS_SECRET_KEY="pick-a-long-random-value-and-keep-it-fixed"  # required — see §1b
./run.sh              # plain start, empty task database
./run.sh --demo-data   # start with clearly-labelled demo fixture tasks
```

### Windows (cmd.exe or PowerShell) — run.sh will not work; use these directly
```bat
cd backend
pip install -r ..\requirements.txt
pip install -r ..\requirements.txt
python main.py
```
PowerShell: `$env:ABPS_SECRET_KEY="pick-a-long-random-value-and-keep-it-fixed"`

To include demo fixture tasks on Windows, also set (uvicorn's own argv
handling means `--demo-data` can't be passed as a plain CLI flag when
launched this way):
```bat
set ABPS_DEMO_DATA=1
python main.py
```
PowerShell equivalent: `$env:ABPS_DEMO_DATA="1"`

If `ABPS_SECRET_KEY` is left unset, the server still starts (a random key is
generated for that process, with a console warning) — fine for a quick
local look, but every issued token is invalidated the moment the server
restarts, and it is not safe for any real deployment.

### Open the app
Browse to **http://localhost:8000** — not `http://0.0.0.0:8000`, which
Windows browsers will refuse to connect to even though that's what the
server log prints. Set a fixed `ABPS_SECRET_KEY` environment variable before
starting (see §1b) — without it, every login is invalidated on restart.

You'll land on a **login screen**. Five real accounts are seeded on first
startup, with their (bcrypt-hashed) passwords printed to the server console
so the app is demoable out of the box:

| username | password | role |
|---|---|---|
| `eng.desk` | `Eng@Railways1` | ENG |
| `td.desk` | `Td@Railways1` | TD |
| `snt.desk` | `Snt@Railways1` | SNT |
| `coa.controller` | `Coa@Railways1` | COA |
| `admin` | `Admin@Railways1` | ADMIN |

**Change these before any real deployment** (`POST /api/auth/change-password`).
The left sidebar shows your real identity and a Logout button; an ADMIN
account additionally gets a "Login as" panel to demo other roles without
their passwords (see §1c — this is not a bypass, it's itself gated and
audited).

The task database starts **completely empty**. Nothing is usable until you
either import a CSV, enter tasks manually, or start with `--demo-data`.

### Upgrading an existing install: restart the server
The frontend (`frontend/index.html`) is read from disk on every page load,
but API routes and database tables are only registered when the server
**starts**. After pulling new backend code, stop the running server (Ctrl+C)
and start it again — otherwise the new UI panels call routes the old process
doesn't have, and show **"Not Found"** (e.g. the Admin "Crew & Resource
Capacity" panel or the Network Map). To check what a running server actually
has: `http://localhost:8000/openapi.json`.

On restart with SQLite, `create_all()` adds any new tables automatically
(e.g. `department_capacity`, `gang_assignments`), default gang capacities are
seeded (ENG 4 / TD 3 / SNT 3), and the network map's geometry cache warms in
the background (a few seconds, once). With PostgreSQL, run
`alembic upgrade head` first. Plans created before the crew feature get gang
labels the first time they're read; re-run the scheduler to get plans that
respect crew limits.

---

## 1b. Authentication & authorization

Earlier revisions of this project had no real authentication: role
switching was a UI-only toggle, and every API endpoint trusted whatever
`X-User-Role` header a client sent. That has been replaced entirely.

### Credentials
- `User.password_hash` stores a **bcrypt** hash (via the `bcrypt` package
  directly — not passlib, which has a known incompatibility with
  bcrypt≥4.1's version-detection shim that made it fail outright in
  testing). Plaintext passwords are never stored anywhere.
- `POST /api/auth/change-password` requires the current password to set a
  new one.

### Sessions — JWT, access + refresh
- `POST /api/auth/login {username, password}` → `{access_token,
  refresh_token, expires_at, user}`.
- Access tokens expire in **20 minutes**; refresh tokens in **7 days**.
  `POST /api/auth/refresh {refresh_token}` mints a new pair and **rotates**
  the refresh token (the old one is immediately revoked) — a stolen,
  already-used refresh token cannot be replayed.
- `POST /api/auth/logout {refresh_token}` revokes that refresh token
  (recorded in the `revoked_tokens` table). Access tokens are deliberately
  **not** checked against a revocation list on every request — that would
  mean a DB lookup per request; they simply expire quickly instead. This
  tradeoff is stated here rather than left implicit.
- `GET /api/auth/me` → current identity `{user_id, name, role, department}`.
- Set `ABPS_SECRET_KEY` to a fixed secret before deploying. If it's unset,
  the server generates a random one at startup **and prints a warning** —
  fine for a local demo (every token just becomes invalid on the next
  restart), never acceptable for a real deployment.

### Rate limiting / lockout
5 consecutive failed passwords locks that account for 15 minutes — even the
*correct* password is rejected while locked. Every failed attempt and the
lockout itself are separate, distinct audit log entries
(`login_failed`, `account_locked`, `login_blocked_locked`). This is
account-level throttling, not IP-level; a reverse proxy or a shared store
(e.g. slowapi + Redis) would be the next layer for that, out of scope here.

### Enforcement: one dependency, applied everywhere
`backend/auth.py` defines exactly two dependencies —
`Depends(auth.get_current_user)` and `Depends(auth.require_role(...))` —
and every route in every router uses one of them. There is no inline
`if role == ...` anywhere in a route body; that is deliberately the one
place these checks are allowed to live, so a missed check can't hide in a
handler. The full route-by-route table is in §1c.

### Row-level scoping
Route-level role checks are not enough on their own — `GET /api/tasks` is
open to any authenticated department user, but the SQL query itself is
filtered by `current_user.department` for ENG/TD/SNT callers, and a
client-supplied `?department=` query parameter is **ignored** for them
(not merely defaulted) — verified live: a TD user passing
`?department=ENG` still gets back only TD's own rows. `GET
/api/schedule/plan` and `GET /api/schedule/comparison` apply the same
scoping to their per-task entry lists (aggregate counts/hours are shown to
everyone; only task-level rows are scoped).

---

## 1c. Route-by-route authorization table

| Route | Who may call it |
|---|---|
| `POST /api/auth/login`, `/refresh` | anyone (that's the point) |
| `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/change-password` | any authenticated user |
| `POST /api/auth/impersonate` | **ADMIN only** — see below |
| `POST /api/tasks` | **ENG/TD/SNT only**, and `department` must equal the caller's own department |
| `GET /api/tasks` | any authenticated user; **row-scoped** to own department for ENG/TD/SNT |
| `GET /api/tasks/template` | public (static template, no operational data) |
| `POST /api/tasks/import` | **ENG/TD/SNT only**, `department` query param must match caller |
| `POST /api/tasks/{id}/flag-safety`, `/flag-interlocking`, `/mutual-exclusion` | task's owning department (ENG/TD/SNT), or COA/ADMIN as an elevated override |
| `POST /api/data/load-timetable` | **COA only** |
| `GET /api/stations`, `/api/timetable`, `/api/timetable/top-corridors` | any authenticated user (real public data, not department-scoped) |
| `GET /api/corridor-availability` | any authenticated user |
| `GET /api/corridor-search` | any authenticated user — every role, not one department (§10b) |
| `POST /api/corridor-availability`, `/derive` | **COA only** |
| `GET /api/freight-forecast` | any authenticated user |
| `POST /api/freight-forecast` | **COA only** |
| `POST /api/schedule/run` | **COA only** |
| `GET /api/schedule/plan`, `/metrics`, `/comparison` | any authenticated user; task-level rows row-scoped for ENG/TD/SNT |
| `POST /api/schedule/approve` | **COA only** |
| `GET/POST /api/admin/*` (users, scoring-config, asset-criticality) | **ADMIN only** |
| `GET /api/admin/audit-log` | **ADMIN and COA** — the one deliberate exception to the blanket admin-only rule above; Control Office needs operational audit visibility without full system-config rights |
| `GET/POST/DELETE /api/admin/weather-rules*` | **ADMIN only** to write (`GET` also allows **COA**, same "elevated visibility, not elevated write access" pattern as `/api/admin/audit-log` and `/api/admin/scoring-source`) — see §9d |
| `GET /api/weather/forecast`, `/api/weather/health`, `/api/weather/assessments` | any authenticated user (no department-scoped task data) |
| `POST /api/weather/ingest` | **COA only** — same tier as loading the timetable / deriving corridor availability |
| `GET /api/admin/department-capacity`, `GET .../impact` | **ADMIN and COA** (COA needs to see why a task was crew-limited) — §11f |
| `POST/PATCH/DELETE /api/admin/department-capacity*` | **ADMIN only**; every change audit-logged with before/after and acting user |
| `POST /api/schedule/manual-assign`, `/manual-swap`, `/entries/{id}/reschedule` | COA/ADMIN (reschedule: COA) — and **rejected** if it would exceed a department's gang capacity, which no override can bypass |
| `GET /api/map/corridors`, `/api/map/stations`, `/api/map/corridors/{id}`, `/api/map/stations/{code}` | any authenticated user; for ENG/TD/SNT, other departments' blocks show department + times only (task/gang details redacted) — §11g |

**A note on COA vs ADMIN**: `/api/schedule/run` and `/api/schedule/approve`
are COA-only, *not* COA-or-ADMIN. This is intentional separation of
duties, not an oversight — ADMIN owns system configuration and user
accounts; COA owns the operational decision to close a corridor. An
account with system-admin privileges should not automatically be able to
publish a live block plan.

### Impersonation ("Login as")
The old role-switcher UI was a genuine demo convenience, so it was kept —
but re-gated. `POST /api/auth/impersonate {target_user_id}` requires an
authenticated **ADMIN** session, issues an **access-only** token for the
target user (no refresh token, so an impersonated session can't be
silently renewed and simply expires in 20 minutes), and always writes a
`user_impersonation_started` audit entry naming both the admin and the
target. It is never reachable without a real ADMIN login first — verified
live: a TD token calling this endpoint gets 403.

---

## 2. Where the real data comes from

### Train timetable, stations, corridors — real, public
Source: the **Indian Railways Train Time Table** catalog on data.gov.in
(https://www.data.gov.in/catalog/indian-railways-train-time-table). That
portal does not expose a stable, unauthenticated bulk-download URL, so this
project fetches the same dataset via the **DataMeet `railways` mirror**
(CC0, gathered from data.gov.in per its own README):
`https://github.com/datameet/railways` — specifically `stations.json` and
`schedules.json`.

Fetch them (already done once for you if `data/raw/*.json` are non-empty):
```bash
curl -o data/raw/schedules.json https://raw.githubusercontent.com/datameet/railways/master/schedules.json
curl -o data/raw/stations.json  https://raw.githubusercontent.com/datameet/railways/master/stations.json
curl -o data/raw/trains.json    https://raw.githubusercontent.com/datameet/railways/master/trains.json
```
Then, as the Control Office user, click **"Load / Refresh Timetable"** (or
`POST /api/data/load-timetable`) to parse them into the database.

**Verified on this build**: loading the real dataset produced **386,656
train-movement segments** across **5,186 distinct real trains** (e.g.
01101, 01102, 01493, 02081, 56044) and **16,848 distinct corridors**, plus
**8,990 real stations**. Corridor IDs (e.g. `THK-KYN`, `GZB-SBB`) are
consecutive from-station→to-station pairs taken directly from the dataset —
none are invented.

Corridor availability is then **derived**, not guessed: `POST
/api/corridor-availability/derive` expands each train's daily schedule
across the requested horizon and finds windows with no scheduled movement.
Example, verified live on this build for corridor `THK-KYN`: a genuine
**2h51m gap from 01:45 to 04:36**, bracketed by real train **11087**
(preceding) and real train **06502** (following), recurring across the week.

*Caveat stated plainly*: the open dataset has no weekly running-days
calendar, so each segment is treated as recurring once per calendar day.
This assumption is documented in `backend/timetable_loader.py` rather than
silently baked in.

### Maintenance defect records — NOT public, must be ingested
TMS (Engineering), SMMS (S&T), and TDMS (Traction Distribution) are internal
Indian Railways systems. No public dataset of real defect records exists, so
**this project never fabricates any**. Instead there are three real ingestion
paths, all wired to the same scoring/optimizer core:
1. **Manual entry** — the task submission form on each department desk.
2. **CSV/Excel bulk import** — see §3 below.
3. **Pluggable adapters** (`backend/adapters/`) — `SourceSystemAdapter` is
   an abstract base class; `TMSAdapter`, `SMMSAdapter`, `TDMSAdapter`, and
   `COAAdapter` are concrete stubs that currently return an empty result set
   and a "not connected" health status. Dropping in a live client for a real
   system later means writing one subclass and registering it — the
   scoring engine, optimizer, and API never change.

### Demo fixture data — off by default, clearly labelled
`--demo-data` (or `ABPS_DEMO_DATA=1`) seeds a handful of maintenance tasks
with `source="demo"`. They are visibly tagged (a red `DEMO` badge in the UI)
and never mixed silently with real records — every task and slot in ABPS
carries its provenance (`manual` / `csv` / `adapter` / `demo`,
`timetable_gap` for corridor slots).

---

## 2b. Layer 2: the data integration pipeline

CSV/Excel import (§3) is not just "parse and insert" — it's five explicit,
inspectable stages in `backend/pipeline/` and `backend/importers/`:

```
ingestion (read file) -> cleaning (dedupe) -> normalization (dept aliases,
ID casing, duration units) -> validation (per-field checks) ->
asset/corridor ID mapping -> the unified database
```

- **Cleaning** (`pipeline/cleaning.py`): drops exact-duplicate rows within
  one file (not cross-batch — two independently submitted reports of the
  same real defect are a business decision, not a data-cleaning one).
- **Normalization** (`pipeline/normalization.py`): unifies department
  aliases ("Engineering"/"TMS" → `ENG`, etc.), uppercases/trims asset and
  corridor IDs, and converts an optional `duration_unit=minutes` column to
  the canonical hours.
- **Asset & Corridor ID Mapping** (`pipeline/id_mapping.py`): TMS, SMMS, and
  TDMS each have their own real-world asset-numbering convention. An
  incoming `asset_id` is resolved in order: (1) an explicit mapping row for
  `(source_system, source_asset_id)`, (2) already-canonical (it matches a
  registered `AssetCriticality.asset_id` directly — the common case for
  manual entry), or (3) **unresolved** → sent to the **review queue**
  (`ReviewQueueItem`), never silently dropped or guessed at. Corridor IDs
  resolve the same way but fall back to freeform passthrough (ABPS doesn't
  maintain a closed corridor master list) unless strict resolution is
  requested.
- **Provenance & pipeline stats**: every import creates an `IngestionBatch`
  row (`rows_in`/`rows_valid`/`rows_rejected`/`rows_review`,
  `GET /api/pipeline/batches`) and every unresolved row becomes a
  `GET /api/pipeline/review-queue` entry — both visible in the Control
  Office's **Data Pipeline** panel.

**Verified worked example** (real, executed): registered
`TMS-RAIL-CH4521 → ENG-TRK-1042` via `POST /api/pipeline/asset-mappings`,
then imported a row whose `asset_id` was the *raw TMS convention*
(`TMS-RAIL-CH4521`) — the created task's `asset_id` came back resolved to
the canonical `ENG-TRK-1042`. In the same session, importing a row with an
unregistered asset (`ENG-TRK-9999`) produced `review_count: 1` and a
review-queue entry with reason *"asset_id 'ENG-TRK-9999' has no known
mapping from TMS to a canonical asset"* — never inserted as a task, never
silently dropped.

### PostgreSQL migration (Alembic)

SQLite remains the zero-config local-dev default (`python main.py` just
works). For Postgres:
```bash
docker compose up -d                                    # starts postgres:16-alpine on :5432
export ABPS_DATABASE_URL=postgresql://abps:abps@localhost:5432/abps
python -m alembic upgrade head                            # from the project root
cd backend && python main.py
```
`main.py`'s startup only runs SQLite's `create_all()` convenience path when
`ABPS_DATABASE_URL` starts with `sqlite`; against any other URL it expects
Alembic to already own the schema, so the two never fight over who creates
what. `migrations/env.py` reads the exact same `ABPS_DATABASE_URL` the app
uses — one source of truth for "which database."

**What was and wasn't verified**: Docker was not available in the sandbox
this project was built and tested in, so a live Postgres instance was
**not** exercised end-to-end. What *was* verified for real: `alembic
revision --autogenerate` against the actual current models correctly
detected and generated all 18 tables from scratch; `alembic upgrade head`
applied that migration to a fresh SQLite file with zero errors; and, later
in the same build, adding two columns to `Station` (`lat`/`lon` — see the
network map, §8c) was captured as a genuine *incremental* migration
(`alembic revision --autogenerate` → "Detected added column 'stations.lat'"
→ "Detected added column 'stations.lon'") and applied in place to the
already-populated dev database without losing any data, which is real
evidence the migration path works correctly, not just the from-scratch
case. The application code path itself (a plain SQLAlchemy connection
string) is identical whether it points at SQLite or Postgres and was not
changed to add Postgres support — only `psycopg2-binary` needed adding as a
driver.

---

## 3. CSV import format

Each department has its own downloadable template:
`GET /api/tasks/template?department=ENG|TD|SNT`, or the "Download
{DEPT} Template" button on that department's desk.

Columns (all departments share the same shape):

| column | required | notes |
|---|---|---|
| `asset_id` | yes | must already be registered under Admin → Asset Criticality |
| `defect_type` | yes | free text, e.g. `rail_fracture`, `ohe_insulator_fault`, `signal_relay_fault` |
| `severity` | yes | integer 1–5 |
| `reported_date` | no | `YYYY-MM-DD`; if given, `overdue_days` is computed from it and the `overdue_days` column is ignored |
| `overdue_days` | no | integer ≥ 0, used only if `reported_date` is blank |
| `required_duration_hours` | yes | float > 0 |
| `corridor_id` | yes | must match a corridor with real timetable entries to be schedulable |
| `safety_critical` | yes | `true`/`false` |
| `interlocking_critical` | yes | `true`/`false` (S&T only, semantically) |
| `mutually_exclusive_with` | no | comma-separated task IDs |

Every row is validated independently. A row with a bad severity, malformed
date, or unregistered `asset_id` is rejected with the exact reason and row
number; the rest of the file still imports. Verified example from this
build: a 3-row file with one bad `severity=9` and one blank `severity`
imported the 1 good row and reported:
```
Row 3: severity must be 1-5, got '9'
Row 4: severity '' is not a valid integer 1-5
```

---

## 4. What's real vs. what requires railway authorization

| Data | Status |
|---|---|
| Train timetable, stations, corridor definitions | **Real**, public (data.gov.in via DataMeet mirror) |
| Corridor availability (traffic gaps) | **Real**, computed from the above |
| Freight forecast | Manual entry only — no public feed exists; enter via Control Office |
| ENG/TD/SNT maintenance defect records | **Requires railway authorization** — TMS/SMMS/TDMS are internal systems. Use CSV import, manual entry, or a live adapter once credentials are available. Never fabricated. |
| Demo fixture tasks | Synthetic, `--demo-data` only, visibly tagged, off by default |
| Weather forecast (§9) | **Real** when the Open-Meteo adapter is reachable (`source="real"`); a clearly-labelled synthetic fallback (`source="synthetic"`, visible as a `SYNTHETIC` badge in the UI) only when it isn't |

---

## 5. Scoring formula

Transparent, weighted, rule-based — not a black-box model:

```
score = w_severity     × severity            (1-5)
      + w_overdue       × min(overdue_days, 60)
      + w_criticality   × asset_criticality   (1-5, default 3 if unregistered)
      + w_safety_flag                          (flat bonus if safety_critical OR interlocking_critical)
```
Defaults: `w_severity=10.0`, `w_overdue=1.5`, `w_criticality=8.0`,
`w_safety_flag=25.0`. Runtime-configurable by an Administrator via
`POST /api/admin/scoring-config` (changing a weight immediately rescores
every task in the system).

Every score ships with a plain-English breakdown, e.g.:
> Priority score 166.5: severity 5/5 contributed 50.0 pts; 45 overdue
> day(s) contributed 67.5 pts; asset criticality 3/5 contributed 24.0 pts;
> safety-critical flag contributed 25.0 pts.

---

## 5b. Layer 3: AI / Analytics — and why the data is synthetic

**Read this before trusting any AUC number below.** Real TMS/SMMS/TDMS
failure history is exactly as unavailable as real defect records (§4) — no
public dataset of "this asset did or didn't fail" exists. Training a risk
model needs that history, so `backend/ml/synthetic_data.py` generates it,
with a **documented, hand-specified causal structure** rather than random
noise, so a model trained on it learns *something* rather than nothing:

```
risk_logit = -3.4 + 0.38×severity + 0.028×age_years + 0.26×criticality
           + 0.018×overdue_days + 0.09×(traffic_density/10)
           + 0.22×historical_failure_count + 0.45×(1 if monsoon month else 0)
           + noise ~ Normal(0, 0.5)
risk_probability = sigmoid(risk_logit)
failed_within_30_days = Bernoulli(risk_probability)      # the classification target
actual_repair_duration_hours = base_by_defect_type + 0.3×severity + noise   # the regression target
```
Every coefficient is a real, inspectable line in that file — failure risk
genuinely rises with severity, age, criticality, overdue days, corridor
traffic, and prior failures, with a monsoon-season bump for track/OHE
assets, matching real domain intuition even though the data itself is
invented. The `Normal(0, 0.5)` noise term is deliberate: an easy synthetic
target would produce a suspiciously perfect model that says nothing
credible about real-world performance.

**Any model-derived number is labelled "trained on SYNTHETIC data" wherever
it appears** — the AI Priority & Risk Queue panel, `GET /api/ml/evaluation`,
every prediction from `GET /api/ml/predict/{task_id}` — never presented as
if it came from operational history.

### 3A — Maintenance risk & priority prediction
`POST /api/ml/train` trains **both** XGBoost and Random Forest on 2000
synthetic records (75/25 train/test split), for both targets, and persists
the better classifier (by test AUC) as the active model:

**Real measured results, this build** (`n_records=2000`, `seed=42`):
| Metric | XGBoost | Random Forest |
|---|---|---|
| Classification AUC (`failed_within_30_days`) | **0.6597** (chosen) | 0.6567 |
| Regression RMSE / MAE (`actual_repair_duration_hours`) | 0.662h / 0.521h | 0.649h / 0.511h |

Ranking correlation between the model's predicted probability and the
**true generating probability** (not the noisy binary label — the honest
way to check whether the model recovered real signal vs. memorized noise):
**0.6914**.

**Honest interpretation, stated plainly**: an AUC of 0.66 is modestly
better than chance (0.5), not dramatically high — exactly what you'd expect
given the deliberately-injected noise, and a more credible outcome than a
suspiciously perfect score would be. `GET /api/ml/evaluation`'s
`limitations` field states this dynamically every time (it checks the
actual observed AUC and writes a different sentence depending on whether it
came back high/modest/near-chance, rather than asserting a canned claim
regardless of outcome) plus the standing caveat that real deployment would
require retraining on actual failure history and re-validating every
coefficient above against real outcomes.

Every live prediction (`GET /api/ml/predict/{task_id}`) includes a
**SHAP-based explanation** of the top 3 contributing features for that
specific task, e.g.: *"traffic_density increased risk by 0.464;
criticality increased risk by 0.374; is_monsoon increased risk by 0.267."*
A score with no explanation is not usable for safety-critical work.

### 3B — Train traffic & corridor forecasting

Built on **real** data, not synthetic — the underlying series is the real
scheduled train movements loaded in §2. **SARIMA** (statsmodels) was chosen
over Prophet: the source dataset has no per-day historical variation (each
real segment recurs identically every calendar day, per §2's documented
assumption), so the only real seasonality to model is the 24-hour daily
cycle — exactly what a SARIMA model's own seasonal term (`seasonal_order=
(1,1,1,24)`) captures directly, with no extra dependency beyond
statsmodels (already installed) and none of Prophet's fragile
cmdstan/pystan build toolchain on Windows.

`GET /api/ml/traffic-forecast?corridor_id=THK-KYN` fits on one real week of
the corridor's schedule and forecasts the horizon forward, reporting
contiguous low-traffic windows bracketed by the **real** trains immediately
before/after (read from the real schedule, not the forecast).

**Verified real example**: forecast for `THK-KYN` found a window
**2026-09-19 02:00–04:00** (0.0 forecast trains/hour), bracketed by real
train **11087** (preceding) and **06502** (following) — the same two real
trains bracketing the directly-derived gap in §2, confirming the model
correctly recovered the real recurring pattern (rounded to hour boundaries,
since the forecast operates on hourly buckets).

### Wiring 3B into the optimizer (Layer 3 → Layer 4)
`POST /api/corridor-availability/derive-ml-forecast` converts forecasted
low-traffic windows into `CorridorSlot` rows tagged
`derived_from="ml_forecast"` — the **same table** real timetable-gap slots
use. No scheduler change was needed to wire this in: a slot is a slot
regardless of how it was derived, and every slot is independently
re-validated against the real timetable before CP-SAT ever sees it
(`scheduler._slot_is_safe`, §6) — so an ML-forecast slot gets exactly the
same safety guarantee as a directly-derived one.

### Configurable scoring source
`GET/POST /api/admin/scoring-source` (ADMIN to change; COA has read-only
visibility, same "elevated visibility, not elevated write access" pattern
as the audit log) switches every task between:
- `rule` — the §5 formula (default, always available, the baseline the ML
  model is compared against).
- `ml` — `ml_priority_score = 100×risk_probability + 0.5×urgency_score +
  0.5×criticality_score`, where `urgency_score = 100×(1 − e^(−overdue/30))`
  is a deliberately different (saturating) curve from the rule's linear-
  capped overdue term, so the two are a genuine second opinion, not the
  same formula twice.
- `blend` — a documented, un-tuned 50/50 average of both.

**Verified live**: a task scored `95.0` (rule) came back `155.83` under
`ml` (*"failure risk 91.7%, urgency 28.4, criticality 100.0"*) and `125.4`
under `blend` (`= 0.5×95.0 + 0.5×155.8`, exact arithmetic match). If `ml`/
`blend` is selected but no model has been trained yet, scoring falls back
to `rule` automatically with the reason appended (`source_used:
"rule_fallback"`) — never a hard failure, matching the same never-fails
philosophy as the scheduler's own CP-SAT→greedy fallback.

---

## 6. The optimizer

The engine is structured as three explicit stages (Layer 4A/4B/4C), even
though 4A and 4B don't need new code of their own — they're satisfied by
how the existing pieces already compose:

- **4A — Candidate generation**: every safe `CorridorSlot`, whether derived
  from a real traffic gap (§2) or an ML-forecasted low-traffic window
  (§5b), re-validated against the real timetable (`get_safe_slots`).
- **4B — Multi-department clustering**: same-corridor, time-compatible,
  resource-compatible (not mutually exclusive) tasks are exactly the
  cross-department pairs the CP-SAT objective's coordination bonus (below)
  rewards for genuinely overlapping — the clustering criterion and the
  reward criterion are the same test, applied inside the solve rather than
  as a separate pre-pass, so a "cluster" is never a suggestion the solver
  might ignore.
- **4C — CP-SAT interval scheduling**: described in full below.

`backend/scheduler.py` models task placement as **true interval scheduling**,
not slot assignment: each task gets a specific start/end time inside a
corridor availability window, rather than occupying the window's entire
duration. This matters because the whole point of a window is that it's
often much longer than any one task needs — a 2-hour task should never
block a second task from using the other 4 hours of a 6-hour window, but
that's exactly what a coarser "assign whole slot" model does.

Everything is worked in **integer minutes from a fixed epoch** (the horizon
start) — no floats or datetimes inside the CP-SAT model itself; conversion
back to real datetimes happens only when writing `BlockPlanEntry` rows.

- For every (task, candidate window) pair where the window's corridor
  matches and is long enough, an **optional interval variable**
  (`NewOptionalIntervalVar`) is created: a start var, a fixed size (the
  task's required duration), an end var, and a presence literal. A task is
  "scheduled" iff exactly one of its candidate presence literals is true.
- **Per-corridor capacity uses `AddCumulative`, not `AddNoOverlap`** — this
  was a deliberate choice. `AddNoOverlap` would forbid any two tasks from
  ever occupying the corridor at the same time, which defeats the entire
  coordination goal (ABPS exists specifically so multiple departments' crews
  *can* work the same closure simultaneously). `AddCumulative` with a
  configurable capacity (default **3** concurrent work parties per corridor,
  `corridor_capacity` query param on `/schedule/run`) lets up to N tasks
  genuinely overlap in time while still capping real-world crew congestion.
- **Per-department crew capacity** is a second, independent `AddCumulative`:
  exactly **one per department**, over that department's candidate intervals
  on **every** corridor (gangs are a department-wide resource), demand 1 per
  task, ceiling = the department's `max_concurrent_gangs`. Both cumulatives
  must hold at once. Per-date overrides are folded into the same single
  constraint with fixed reservation intervals — details in §11f. It is
  **hard**: the greedy fallback and every manual-override path enforce it too.
- **Mutual exclusion** (`mutually_exclusive_with`) is enforced with
  `AddNoOverlap` over the *combined pool* of both tasks' candidate intervals
  across every window either could land in — not by forbidding a shared
  slot. Optional intervals that end up absent are automatically ignored by
  `AddNoOverlap`, so this correctly stops the two tasks overlapping in time
  no matter which window each is ultimately placed in.
- **Coordination is redefined around genuine temporal overlap.** For every
  pair of same-corridor tasks from different departments, a
  one-directional reified `overlap` boolean can only be set to 1 by the
  solver if both tasks are actually scheduled *and* their intervals
  actually overlap in time — the objective rewards `+40` points per such
  pair, so the solver is rewarded for real overlap, not a shared nominal
  window. (Reward is per overlapping pair, so a 3-department window earns
  3 pairwise bonuses, scaling naturally with how much genuine coordination
  is achieved.)
- **Hard safety constraint**: every candidate window is re-validated
  directly against the loaded real timetable before being offered to the
  solver — not just trusted because it was originally derived from a gap.
  Because a task's interval is always constrained to sit fully inside
  whichever window it's placed in, filtering unsafe windows here is
  sufficient to guarantee no interval ever overlaps a real train movement.
- Time-boxed to 25s (`SOLVE_TIME_LIMIT_SECONDS`), comfortably under the 30s
  requirement — verified at both weekly and monthly horizons (§8).
- If OR-Tools is unavailable or the solve throws, it **falls back
  automatically** to a deterministic greedy heuristic that also does real
  interval placement (not whole-window assignment): highest priority task
  first, scanning candidate start times within each eligible window
  (window start plus every existing interval's end time), picking the
  earliest one that respects both the corridor's concurrency capacity and
  every mutually-exclusive partner already placed anywhere. Never hard-fails
  — a task simply stays unscheduled if no window has room.
- Both `weekly` (7-day) and `monthly` (30-day) horizons are supported from
  the same data.
- Tasks that don't fit are never silently dropped. Each gets one of two
  distinct, specific reasons:
  - *"No corridor slot on {corridor} long enough for the required {N}h
    within this horizon (corridor unavailability)."*
  - *"Lower relative priority than competing tasks contending for the same
    available slots."*

`BlockPlanEntry.assigned_window_start/end` store the actual computed
interval times, not the enclosing window's times — so the corridor timeline
visibly shows multiple tasks sitting at different offsets within one window.

---

## 6b. Before vs after: the manual-process baseline

`GET /api/schedule/comparison?horizon=weekly|monthly` answers "how much
better is this than what departments do today?" by simulating the existing
manual process (`backend/baseline.py`) and comparing it against the
optimizer's actual plan, using **identical metric definitions** for both
(`backend/metrics.py`) so the comparison is apples-to-apples.

**Baseline assumptions** (deliberately conservative, not a strawman — full
rationale in `backend/baseline.py`'s docstring):
- Departments act independently and **sequentially**: ENG first, then TD,
  then SNT — modelling "no cross-department awareness" as whichever
  department's request lands on a window first claims it outright.
- Within a department, tasks are processed in **submission order**
  (`created_at`), not priority order — a manual process is
  first-come-first-served, not risk-ranked. Sorting the baseline by priority
  would hand it foreknowledge the real process doesn't have.
- Each task claims the **earliest remaining window** on its corridor that
  fits its duration, and reserves that window's **entire span exclusively**
  for its own department — once claimed, no other task from any department
  may use any leftover time in it. This single rule is the whole
  inefficiency ABPS eliminates.
- The same real-timetable safety filtering applies, so neither side is
  handicapped by an unfair pool of windows.

**Metrics** (both engines, same definitions): `tasks_total`,
`tasks_completed`, `total_corridor_closures` (distinct windows used),
`total_downtime_hours` (baseline: the window's *full* duration per closure,
since it reserves the whole thing; optimized: the genuine span from the
earliest task's start to the latest task's end in that window — the honest
cost of the closure actually requested), `priority_weighted_completion`,
`block_utilization_pct` (productive work-hours ÷ total available
block-hours, measured in hours, not slot count), `coordinated_blocks`
(windows with 2+ departments — structurally always 0 for the baseline, by
construction), and `avg_overdue_days_of_scheduled_tasks`. The endpoint
response also includes `delta` (absolute + percent per metric) and
`baseline_entries` (for the frontend's dual timeline).

The Control Office "Before vs After" panel renders both schedules on the
same corridor timeline, baseline stacked above optimized, so the packing
density difference is immediately visible — see the real example in §8.

---

## 6c. Layer 5: Decision Intelligence — the differentiating layer

Four capabilities in `backend/decision_intelligence.py`, all COA-only, all
built on `scheduler.simulate_schedule()` — a **read-only** twin of
`run_schedule` that solves and computes metrics without ever calling
`_materialize_plan`, so exploring "what if" can never corrupt the real
draft/published plan or task state.

### Emergency re-optimization (with a real deviation penalty)
`POST /api/decision/emergency-reoptimize` injects an urgent defect and
re-solves with a **stability bonus** added directly to the CP-SAT
objective: for every task in the currently-published plan, a reified
`unchanged` boolean can only be set to 1 if that task is still scheduled at
*exactly* its previous start time, and each `unchanged=1` earns a bonus
(default 60 points). This is the same reification pattern as the
coordination bonus (§6) — one-directional, so the solver is rewarded for
stability but never forced to claim it falsely. Gangs who already planned
around the published schedule aren't disrupted unless the emergency's own
priority genuinely outweighs the cost of moving things.

**Verified real result**: injecting a severity-5 emergency rail-fracture
task against a published 11-task weekly plan — the emergency task was
scheduled (`emergency_task_scheduled: true`) and **all 11** previously-
published tasks stayed exactly where they were (`tasks_unchanged: 11,
tasks_moved: []`). Zero disruption for a genuine emergency, because there
was room; the deviation penalty is what makes that room-finding preferred
over reshuffling by default.

### What-if scenario analysis
`POST /api/decision/what-if` runs N named scenario configs
(`corridor_capacity`, `exclude_corridors`, `coordination_bonus`,
`extra_slot_hours`) through `simulate_schedule` and returns them side by
side — nothing is ever persisted.

**Verified real result**, three scenarios on the same task/window set:
| Scenario | Scheduled | Coordinated blocks | Closures |
|---|---|---|---|
| Capacity 3 (current) | 12 | 2 | 2 |
| Capacity 1 | 12 | 0 | 8 |
| Corridor closed | 0 | 0 | 0 |

A real bug was caught and fixed while verifying this: `metrics.py`'s
`coordinated_blocks` originally counted "2+ departments share the same
nominal window `slot_id`", which is **not** the same thing as genuine
temporal overlap under true interval scheduling — with capacity forced to
1, two different-department tasks can still land in the same window
back-to-back with zero actual overlap, and the old logic still counted that
as "coordinated." Fixed to require an actual overlapping pair
(`a.start < b.end and b.start < a.end`) between different departments
within the window, matching the definition `co_scheduled_departments`
already used elsewhere (§6) — after the fix, capacity=1 correctly reports
**0** coordinated blocks (real temporal overlap becomes impossible at
capacity 1), which is what caught the inconsistency in the first place.

### Explainability: "why this block?" and real counterfactuals
`GET /api/decision/explain/{task_id}` — for a **scheduled** task, states
which corridor/window/capacity/timetable-safety facts placed it there and
names any co-scheduled departments. For an **unscheduled** task, it names
the lowest-scoring competing task actually occupying that corridor (when
one exists) and runs **real counterfactual re-solves** (not guesses) by
temporarily mutating the task's severity or duration in the same DB
session, re-solving, and always rolling back:

**Verified real example** — a 3-hour task unschedulable for "corridor
unavailability" (longest window is 2.85h):
- *severity bumped to 5* → re-solved → **still not scheduled** (correctly:
  the problem is corridor time, not priority contention — more priority
  can't manufacture more corridor-hours).
- *duration halved to 1.5h* → re-solved → **would be scheduled** (a 1.5h
  window genuinely exists).

A real bug was caught and fixed here too: the first version only attempted
counterfactuals when the task's *original* duration already had at least
one fitting window — which meant it silently skipped testing the duration
counterfactual in exactly the "corridor unavailability" case where it
matters most. Fixed to always attempt both counterfactuals; the duration
one now correctly demonstrates real, actionable advice for the case above.

### Shadow prices
`GET /api/decision/shadow-price?corridor_id=&extra_hours=` solves once
as-is and once with every safe slot on that corridor extended by
`extra_hours`, reporting the difference.

**Verified real result**: *"4 more hour(s) on THK-KYN would allow 4 more
task(s) worth 629.0 priority points."* — turning the system from a
scheduler into an advisor, exactly the example format the SRS asks for.

---

## 7. Governance

- **ML never schedules anything**: the risk model and traffic forecaster
  (§5b) only ever produce a probability, a score, or a candidate window —
  every one of those is fed into CP-SAT as a plain number or an ordinary
  `CorridorSlot` row, and CP-SAT is the only thing that ever decides which
  task gets which time. Turning on `ml`/`blend` scoring changes a task's
  `priority_score` input; it never bypasses the solver, the safety
  constraint, the capacity constraint, or the approval gate below.
- **Human-in-the-loop**: `POST /api/schedule/run` always writes a new
  `draft` `BlockPlan` version. Nothing is final until `POST
  /api/schedule/approve {decision: "approve"}`, which publishes it and
  supersedes whatever was previously published for that horizon.
- **Fail-safe**: a run failure never touches the last published plan —
  `run_schedule` only ever inserts a new plan row; it has no code path that
  mutates an existing one.
- **Audit trail**: every task submission, CSV import, flag change, corridor
  slot change, scoring-config change, scheduler run, approve/reject, login,
  failed login, lockout, logout, password change, and impersonation writes
  an append-only `AuditLog` row with timestamp and the **real authenticated
  user** — never a client-supplied field. Visible under Admin → Full Audit
  Trail (and to COA via `GET /api/admin/audit-log`, its one explicit
  exception — see §1c).
- **RBAC**: enforced server-side via the `auth.require_role(...)` /
  `auth.get_current_user` dependencies described in §1b/§1c, never by
  trusting a client-supplied header — department roles (`ENG`/`TD`/`SNT`)
  can only submit/view their own department's tasks; `COA` and `ADMIN` get
  cross-department visibility and their own, separated control-plane
  actions (§1c explains why COA and ADMIN are not treated as
  interchangeable superusers).
- **Data provenance**: every `MaintenanceTask` carries `source`
  (`manual`/`csv`/`adapter`/`demo`) and `source_ref`; every `CorridorSlot`
  carries `derived_from` (`timetable_gap`/`manual`). Both are visible in the
  UI (badges on the task table, `derived_from` on the timeline).

---

## 8. Verified end-to-end (real measured numbers, this build)

1. **Real timetable loads**: 386,656 segments, 5,186 trains, 16,848
   corridors, 8,990 stations. Sample train numbers: 01101, 01102, 01493,
   01494, 02081, 02082, 02083, 02084.
2. **Real derived gap**: THK-KYN, 2026-09-11 01:45 → 04:36 (2.85h), after
   train 11087, before train 06502 — recurring daily across the week (14
   gaps found over 7 days).
3. `GET /` returns HTTP 200 and serves the dashboard.
4. CSV import: 1 good row imported, 2 deliberately malformed rows rejected
   with row-specific reasons (bad severity value, blank severity).
5. `POST /api/tasks` → score 166.5 with justification: *"severity 5/5
   contributed 50.0 pts; 45 overdue day(s) contributed 67.5 pts; asset
   criticality 3/5 contributed 24.0 pts; safety-critical flag contributed
   25.0 pts."*
6. **Genuine interval packing, real example** — 6 tasks from ENG/TD/SNT all
   landed in the *same* THK-KYN window (01:45–04:36) at different, mostly
   non-overlapping times:
   ```
   task-TD-c58206af   TD   01:45–02:15
   task-ENG-097a6640  ENG  01:45–02:45
   task-TD-db5d939f   TD   02:15–03:15
   task-SNT-fa477d0b  SNT  02:45–03:45
   task-ENG-6fa475ea  ENG  03:15–04:15
   task-demo-SNT-005  SNT  01:45–03:45
   ```
   A later, larger run packed **7 tasks across all 3 departments into one
   2h51m window** (01:57–04:36), every single one showing genuine
   cross-department overlap in `co_scheduled_departments`.
7. **Cross-department temporal overlap, real example**: `task-ENG-097a6640`
   (ENG, 01:45–02:45) genuinely overlaps `task-TD-c58206af` (TD, 01:45–02:15)
   and `task-TD-db5d939f` (TD, 02:15–03:15) in the same window — confirmed
   via `co_scheduled_departments`, not inferred.
8. **Mutual exclusion holds under intervals**: two mutually-exclusive tasks
   (`task-TD-db5d939f` 02:15–03:15 and `task-ENG-6fa475ea` 03:15–04:15) were
   placed adjacent, not overlapping — verified with an explicit pairwise
   overlap check across every plan entry: **0 violations**, in both the
   CP-SAT and greedy-fallback paths (greedy tested directly).
9. **Zero timetable violations**: explicitly checked every scheduled
   interval in a live plan (26 entries) against real train occurrences on
   its corridor — **0 overlaps**.
10. **Capacity respected exactly**: max concurrent tasks on THK-KYN never
    exceeded the configured capacity of 3, verified by scanning every
    interval-boundary timepoint across the whole plan.
11. **Solve time within budget**: weekly horizon **0.65s** on a small
    instance (`OPTIMAL`) up to **~22.4s wall-clock** on the 31-task,
    14-window contested instance (`FEASIBLE` — CP-SAT's internal time
    budget was reached, a legitimate, honestly-reported outcome for a
    harder combinatorial instance, not a failure); monthly horizon (60
    windows) **~24.8s wall-clock**. `SOLVE_TIME_LIMIT_SECONDS` is
    deliberately set to **20s**, not 25s, because CP-SAT's own time limit
    doesn't cover model-build and plan-materialization overhead — measured
    up to ~5s on the largest instance tested, which pushed a 25s internal
    budget to 29.8s wall-clock, uncomfortably close to the 30s ceiling. At
    20s, both horizons land with several seconds of margin.
12. **Distinct unscheduled reasons**, both reproduced live: a 50h task on a
    corridor whose longest gap is 2.85h → "corridor unavailability"; the
    lowest-priority tasks losing a capacity-3 contest → "lower relative
    priority."
13. **Approve flow**: `draft` → `published` confirmed via API and the UI;
    `schedule_run` and `schedule_plan_approved` audit rows both landed with
    timestamps and the acting user.
14. **Baseline vs optimized comparison** — run under genuine scarcity (31
    tasks, only 14 available windows/week on THK-KYN), so baseline's
    whole-window-per-task waste actually exhausts the window pool:

    | Metric | Before (manual) | After (ABPS) | Delta |
    |---|---|---|---|
    | Tasks completed | 14 | 26 | **+85.7%** |
    | Corridor closures | 14 | 5 | **−64.3%** |
    | Downtime hours | 33.83 | 9.58 | **−71.7%** |
    | Priority-weighted completion | 985.5 | 1859.0 | **+88.6%** |
    | Block utilization (hours) | 39.9% | 78.3% | **+96.2%** (nearly double) |
    | Coordinated blocks | 0 | 5 | 0 → 5 |

    (Monthly horizon, 60 available windows instead of 14, shows the same
    pattern on the closure/downtime axes: 26 closures → 5 (**−80.8%**),
    62.83h → 9.73h downtime (**−84.5%**) — though tasks_completed and
    utilization come out identical between baseline and optimized at that
    horizon, for the same honest reason explained below: 60 windows is
    enough that baseline never runs out and eventually schedules the same
    task set, just far more wastefully per closure.)

    The optimizer genuinely beats the baseline on every axis. Utilization
    was investigated honestly: in a *low-demand* scenario (few tasks,
    abundant windows) it initially came out identical between baseline and
    optimized, because with `productive_hours / total_available_hours`,
    both terms depend only on which tasks got scheduled, not how tightly
    packed — and when demand is low enough that baseline never actually
    runs out of separate windows, it eventually schedules the same task set
    the optimizer does, just far more wastefully per closure. That's not a
    flaw in the metric; the closure-count and downtime-hour metrics already
    show the difference in that regime. The scarce-resource scenario above
    is the fairer, more realistic test — real corridors are a genuinely
    contested resource — and it's where utilization diverges too. A separate
    bug was also caught and fixed during this verification: utilization's
    "productive hours" was initially computed from each entry's raw
    `end − start`, which for a baseline entry (which spans the *whole*
    reserved window by design) silently counted reserved-but-idle time as
    "productive" and inflated baseline's apparent utilization. Fixed by
    always using the task's real `required_duration_hours` as productive
    time, regardless of how much of the window an engine actually consumed
    around it (`backend/metrics.py`).
15. **Frontend timeline renders at correct offsets**: the Corridor Timeline
    and the Before/After dual timeline both position and size blocks from
    the plan entries' real `assigned_window_start/end` — confirmed visually
    (Playwright screenshot) showing multiple, differently-sized, differently
    colored department blocks packed within one window's width in the
    optimized row, versus one solid full-width block per window in the
    baseline row above it.

---

## 8b. Security verification — every check actually executed, real status codes

Run fresh, end-to-end, on a clean database, by attempting to break the
enforcement described in §1b/§1c (not by inspecting the code and assuming):

| # | Check | Result |
|---|---|---|
| 1 | `POST /api/schedule/approve` with no token | **401** |
| 2 | Same call with a valid ENG token | **403** |
| 3 | Same call with a valid COA token | **200**, plan published |
| 4 | TD user, `GET /api/tasks?department=ENG` | returned rows: `{'TD'}` only, count matched TD's own tasks — the query param was ignored, not merely overridden |
| 5 | TD user, `POST /api/tasks {department:"ENG", ...}` | **403**, `"you are TD; you may not submit a task for department 'ENG'"` |
| 6 | TD user (non-admin), `GET /api/admin/users` | **403** |
| 7 | Audit log after the approval in check 3 | `schedule_plan_approved -> coa.controller` — the real authenticated user, not a placeholder |
| 8 | `SELECT password_hash FROM users WHERE user_id='admin'` | `$2b$12$TGxWkWac.Sseb...` — a real bcrypt hash, confirmed not plaintext |
| 9 | Tampered token (flipped last base64 char) | **401** |
| 9b | Forged token claiming `role=ADMIN`, signed with a guessed key | **401** — signature verification holds |
| 9c | Correctly-signed token with `exp` one hour in the past | **401** |
| 10 | 5 consecutive wrong passwords against `snt.desk`, then a 6th attempt with the *correct* password | attempts 1–5: **401** each, individually audit-logged (`login_failed`, attempt_count 1→5); the `account_locked` event fires exactly on attempt 5; attempt 6 (correct password): still **401**, `login_blocked_locked`, audit-logged |

Also exercised beyond the required list: refresh-token **rotation** (reusing
a just-refreshed token returns 401 `"refresh token has been revoked"`),
`POST /api/auth/impersonate` succeeding for ADMIN and returning 403 for TD,
and `POST /api/auth/change-password` rejecting an incorrect current
password with 401.

**Frontend-level confirmation** (not just the API): driven with Playwright,
not merely read as code —
- A CSS bug was caught and fixed during this verification: `#login-screen`
  and `#app-shell` each had an explicit `display:flex` rule, which (being
  an ID selector) outranks the browser's default `[hidden]{display:none}`
  rule, so toggling the `hidden` attribute silently did nothing and both
  screens rendered stacked on top of each other. Fixed with explicit
  `#id[hidden]{display:none}` overrides.
- With a TD session active, calling the shared `api()` helper against an
  admin-only route in the browser's own JS context returned: *"You don't
  have permission to do that: role 'TD' is not permitted to call this
  endpoint (requires ['ADMIN'])"*.
- Forcing a garbage access token with no refresh token, then calling
  `api('/api/auth/me')`, returned *"Session expired — please log in
  again."* and the login screen was confirmed visible immediately after.

---

## 8c. Frontend UX

The dashboard is still a single vanilla HTML/CSS/JS file with no build step
— everything below is plain DOM code, no framework.

- **Loading / error / empty states everywhere.** Every panel that fetches
  data goes through a shared `loadSection()`/hand-rolled loader pattern:
  a skeleton while loading, a real error box with a working **Retry**
  button on failure (never a silently blank panel), and an empty state that
  explains what to do next. `api()` tags every thrown error with `.status`
  (`0` for a network-level failure, distinct from a real `404`) so a panel
  can tell "the backend is down" apart from "there's legitimately nothing
  here yet." Verified by killing the backend mid-session: every dependent
  panel showed a red error box with Retry, and the sidebar's API lamp
  turned red — nothing went blank.
- **Toasts, not `alert()`.** `toast(msg, kind)` (`ok`/`err`/`info`/`warn`)
  is the only feedback mechanism in the app; stackable, auto-dismissing.
  There are no `alert()` calls anywhere to replace.
- **Confirmation dialogs** (`confirmDialog()`, a theme-matched modal, not
  `window.confirm()`) gate irreversible actions — publishing a plan shows
  scheduled/coordinated/unscheduled counts before committing; rejecting a
  draft or a bulk task-flagging action get the same treatment.
- **Scheduler run feedback**: the Run button disables itself and cycles
  through real stage text ("Scoring tasks…" → "Building the constraint
  model…" → "Solving…") with a live elapsed-time counter, ending in the
  actual returned counts — not a fake progress bar.
- **Interactive corridor timeline** (`renderMainTimeline`/`drawMainTimeline`
  in the JS): click (or focus + Enter) a block for a detail panel with the
  task, its priority score and full justification, and which departments
  share the window; Day/Week/Month zoom with Earlier/Today/Later paging; a
  live "now" marker; real train movements still render as the background
  layer. **Drag a block to a new time** and the backend re-validates it
  against real trains, corridor capacity, and mutual exclusion
  (`POST /api/schedule/entries/{id}/reschedule`, §10) — a rejected move
  snaps back and shows the specific reason (e.g. *"would overlap real
  train 12141 (00:15–00:18)"*), verified live. Dragging is only offered to
  COA on a still-draft plan; a published plan is frozen.
- **Filters (FR-DASH-05)**: department / corridor / priority-tier
  (Critical/High/Medium/Low, derived from the priority score), combinable,
  with active-filter chips and one-click clear — used on both the task
  tables and the Control Office's unscheduled-tasks table.
- **Export (FR-DASH-06)**: `GET /api/schedule/plan/export?format=csv|pdf`
  (§10) downloads the current plan — task IDs, assigned windows,
  departments, co-scheduling groups — as CSV or a formatted PDF (reportlab,
  landscape, wrapped columns). Row-scoped the same way the rest of the
  plan API is.
- **Tables**: sortable columns (click a header, arrow indicates direction),
  client-side search, checkbox bulk-select with a bulk-action bar (e.g.
  flag several tasks safety-critical at once, itself gated by a confirm
  dialog), and pagination once a table exceeds its page size — one shared
  `renderSortableTable()` component used everywhere a table appears.
- **Keyboard shortcuts** in the Control Office view: `R` runs the
  scheduler, `A` approves the current draft, `?` opens a shortcuts help
  overlay — suppressed while focus is in a text field so they never hijack
  typing.
- **Accessibility**: the existing dark palette already meets WCAG AA for
  every text/background pair used (verified by computing contrast ratios —
  the lowest is 5.17:1, well above the 4.5:1 threshold); a global
  `:focus-visible` ring on every interactive element (confirmed visible via
  a keyboard-only walkthrough); ARIA roles/labels on the status lamps and
  every timeline block (`role="button"`, `tabindex="0"`, a label naming the
  task/department/time); the entire login → run-scheduler workflow is
  operable with only Tab/Enter, verified end to end.
- **Narrow widths**: the sidebar stacks above the main content and forms
  collapse to one column under ~860px; verified at 400px width with zero
  horizontal page overflow (only wide tables scroll internally, as intended).

A pre-existing, previously-invisible bug was found and fixed while building
the interactive timeline: the backend sends naive datetime strings with no
timezone marker, and handing them straight to `new Date(...)` makes the
browser parse them as **local** time rather than UTC — on a non-UTC machine
this silently shifted every block by hours and sometimes onto the wrong
calendar day, occasionally pushing it clean out of the visible range. Fixed
with one shared `parseServerDt()` that always parses these strings as UTC.

---

## 8d. Layers 1–6 verification (real measured numbers, this build)

Executed fresh against a running server, not inferred from reading the code:

1. **Pipeline ingestion + malformed row + review queue**: a 3-row CSV
   (`rows_in: 3`) produced `imported_count: 1`, `rejected_count: 1` (*"Row
   3: severity must be 1-5, got '9'"*), `review_count: 1` (*"asset_id
   'ENG-TRK-9999' has no known mapping from TMS to a canonical asset"*) —
   all three outcomes in one real run, none silently dropped.
2. **Asset ID mapping, worked example**: registered
   `TMS-RAIL-CH4521 → ENG-TRK-1042`; a row submitted with the raw TMS ID
   resolved to the canonical asset on the created task. Confirmed via
   `GET /api/tasks`, not assumed.
3. **Real timetable**: 386,656 segments, 5,186 trains, 16,848 corridors,
   8,990 stations — same real dataset as §2, reloaded fresh this session.
4. **ML training, real metrics**: XGBoost chosen (AUC 0.6597 vs Random
   Forest's 0.6567), regression RMSE 0.662h/MAE 0.521h, ranking correlation
   with true generating probability 0.6914. Reported honestly as "modestly
   better than chance," not glossed as high-performing.
5. **Traffic forecasting**: real low-traffic window 2026-09-19 02:00–04:00
   (0.0 forecast trains/hr) bracketed by real trains 11087/06502 — matching
   the directly-derived gap in §2.
6. **Interval packing**: 7 real tasks across ENG/TD/TD/SNT/ENG/TD/ENG all
   landed inside one THK-KYN window at staggered, mostly non-overlapping
   times (01:47–04:17 span) — see §8 item 6 for the full breakdown.
7. **Utilization before/after**: a genuine performance bug was found and
   fixed while measuring this (see item 11) — after the fix, comparison
   figures match §8 item 14.
8. **Cross-department coordinated block**: `task-ENG-*` (03:00–04:00)
   confirmed sharing its window with SNT and TD via `co_scheduled_
   departments`, and the network map's corridor table shows THK-KYN with
   12 active blocks across `[ENG, SNT, TD]`.
9. **Zero timetable violations**: 0 overlaps across every entry in a fresh
   11-entry plan, verified by direct comparison against real train
   occurrences (not sampled).
10. **Mutual exclusion holds**: the one true mutex pair in the plan
    (`task-TD-*`/`task-ENG-*`) showed `overlap=False` under explicit
    pairwise checking.
11. **Solve time — a real bug found and fixed**: the monthly-horizon run
    initially measured **34.99s wall-clock** (over the 30s budget) even
    though the reported `solve_time_seconds` was only 20.16s. Root cause:
    `get_safe_slots()` recomputed a corridor's *entire* real-train
    occurrence expansion independently for **every slot**, rather than once
    per corridor — cheap with ~14 slots, measured at **13.6 seconds** in
    isolation once the ML-forecast layer (§5b) pushed slot counts to 90.
    Fixed by caching each corridor's occurrence expansion once and reusing
    it across all of that corridor's slots: `get_safe_slots()` dropped to
    **0.44s** for the same 90 slots, and the full monthly run measured
    **21.0s wall-clock** afterward — comfortably under budget. Weekly:
    **20.6s**. Both runs returned `FEASIBLE` (not `OPTIMAL`) on this
    session's larger, accumulated 15-task test instance — confirmed
    honestly as a real, expected CP-SAT outcome for a harder instance, not
    a defect: the same code proves `OPTIMAL` in **0.025s** on a 5-task
    subset of the same data.
12. **Emergency re-optimization**: real deviation-penalty result —
    emergency task scheduled, **11 of 11** previously-published tasks
    unchanged, 0 moved.
13. **What-if analysis**: 3 scenarios, genuinely differing results (12/2/2
    scheduled/coordinated/closures at capacity 3; 12/0/8 at capacity 1;
    0/0/0 with the corridor closed) — a real `coordinated_blocks`
    inconsistency was caught and fixed here too (see §6c).
14. **Shadow price**: *"4 more hour(s) on THK-KYN would allow 4 more
    task(s) worth 629.0 priority points"* — a real, sensible marginal-value
    statement from two genuine CP-SAT solves.
15. **Baseline vs optimized**: closures −75%, downtime −81%, coordinated
    blocks 0→2, same task-completion count (explained honestly in §8 item
    14 — abundant window supply in this scenario means baseline eventually
    fits everyone too, just far more wastefully).
16. **Approve flow**: `draft → published` confirmed via API, audit log
    shows the real acting user (`coa.controller`), not a placeholder.

**Real bugs found and fixed while producing the numbers above** (not
glossed over): a `NameError` crash in task submission after a scoring
refactor left a stale variable reference; a counterfactual explanation that
silently skipped the one case (duration reduction under corridor
unavailability) where it mattered most; the `coordinated_blocks` metric
counting shared-window presence instead of genuine temporal overlap; and
the `get_safe_slots()` performance bug above that would have pushed the
monthly scheduler over its 30-second budget under load. Each was diagnosed
from a real symptom (a 500 error, a suspicious empty result, an internally
inconsistent number, a timing measurement), fixed, and re-verified — not
inferred from reading the code.

---

## 9. Weather-aware scheduling

Weather affects real railway maintenance three ways — **safety** (lightning/
high wind near height/live-equipment work; extreme heat and rail-buckling
risk; heat-stress limits), **duration** (signal/track work genuinely runs
slower in fog or heavy rain), and **priority** (drainage/embankment/
corrosion-prone-OHE defects become more urgent specifically *because* severe
weather is forecast) — plus real trains slow or hold in fog/heavy rain,
changing what a "low-traffic window" actually means. This is an **additive**
capability layered across the existing Data Sources / Integration / Analytics
/ Optimizer / UI layers — no existing file was rewritten to add it.

### 9a. Data source (Layer 1) — real, with honestly-labelled synthetic fallback

`WeatherForecastEntry` is the 7th ingested source, alongside TMS/SMMS/TDMS/
COA/timetable/freight-forecast: corridor_id, forecast_date, precipitation_
probability_pct, wind_speed_kmh, visibility_km, fog_risk, lightning_risk,
temperature_max_c/min_c, source (`real`|`synthetic`), provider, fetched_at.

`backend/adapters/weather.py` follows the exact same pluggable-adapter
pattern as `SourceSystemAdapter` (`backend/adapters/base.py` now also defines
`WeatherSourceAdapter`):
- **`OpenMeteoAdapter`** (real) — [Open-Meteo](https://open-meteo.com) is a
  free, public, documented weather API with genuine India-region coverage
  and no API key required, used here as the "documented third-party weather
  API providing India-region forecasts" the data policy allows as an
  alternative to IMD's own API (which, like data.gov.in's timetable catalog
  in §2, has no stable unauthenticated bulk-forecast endpoint this project
  could integrate against directly).
- **`SyntheticWeatherAdapter`** (synthetic) — deterministic, seeded per
  corridor+date, used **only** when the real adapter raises, and every row it
  produces carries `source="synthetic"` — never silently substituted, never
  shown as real. `GET /api/weather/health` reports both adapters' live
  connectivity.

**Verified real fetch, this build**: `POST /api/weather/ingest
{"corridor_id":"GZB-SBB","days":16}` resolved the real station GZB
(lat 28.6497, lon 77.4311, from the loaded stations dataset) and returned 16
days of genuine Open-Meteo data, e.g. **2026-09-12: 86% precipitation
probability, 13.9 km/h wind, lightning risk = true** (WMO weathercode 96,
thunderstorm) — `source: "real"`, `provider: "open-meteo"` on every row.

### 9b. Integration (Layer 2)

`weather_service.nearest_station_for_corridor()` resolves a corridor to a
station using the corridor_id's own `{FROM}-{TO}` structure (itself derived
directly from real timetable segments, §2) — the forecast is fetched against
the corridor's **origin** station's real coordinates. `_validate_row()`
rejects impossible values (precipitation outside 0–100%, negative wind speed,
`temperature_min_c > temperature_max_c`, temperatures outside ±60°C) before
anything is stored. Provenance (`source`/`provider`) is carried on every row
exactly like `MaintenanceTask.source` and `CorridorSlot.derived_from`.

### 9c. Analytics (Layer 3)

**3A — Priority scoring** (`priority_engine.py` + `weather_service.
weather_priority_bonus`): a new weight, `w_weather_risk` (default 1.0,
admin-tunable exactly like `w_severity` etc.), applies when a task's
`defect_type` matches a **configurable** weather-sensitive rule (mode=
`priority` in the table below — not a hardcoded category list) *and* the
matching hazard is forecast on the task's corridor within the reliable
forecast horizon. Never a silent multiplier — the justification string
always names it:

> **Verified real example**: a TD `ohe_insulator_fault` task on GZB-SBB
> scored *"...asset criticality 3/5 contributed 24.0 pts. Forecasted heavy
> rain (86% probability) today on GZB-SBB added 10.0 pts to heavy rain
> forecast raises corrosion-prone ohe urgency."* A control task with an
> unrelated defect type (`cable_joint_test`) on the **same corridor, same
> forecast** scored severity+overdue+criticality only, with **zero** weather
> contribution — confirming the effect is genuinely category-gated, not a
> blanket bonus.

**3B — Traffic/low-traffic-window forecasting** (`ml/traffic_forecast.py`):
the SARIMA forecast (§5b) models train **count**, not train **punctuality**
— it has no way to know fog/heavy rain makes real trains run late. Rather
than fabricate a delay model with no real delay-history data to train it on
(same data-availability constraint as §5b's honesty note), each low-traffic
window found is **padded inward by a configurable margin (default 30 min per
edge)** on any day its span forecasts fog or >60% rain probability — a late
train just before or just after a nominally quiet window can bleed real
occupancy into it, so narrowing the window is the same conservative
"don't trust the clear-weather assumption" move as the optimizer's own soft
duration buffer (§9d). A window that shrinks below `min_gap_hours` after
padding is dropped entirely rather than offered unsafely.

> **Verified real example**, THK-KYN, real rain forecast 67–94% for
> 2026-09-19 through 2026-09-25: every SARIMA-found low-traffic window
> (schedule-only duration 2.0h each) was narrowed to **1.0h**
> (`weather_adjusted: true`, `schedule_only_duration_hours: 2.0`,
> `duration_hours: 1.0`), with the specific forecast value named in each
> window's `weather_note`.

### 9d. Optimizer (Layer 4) — the safety-critical part

`weather_service.assess_candidates()` runs before `scheduler.py` builds any
CP-SAT/greedy candidate, using a single admin-editable table
(`WeatherSensitivityRule` — `defect_type_pattern`, `hazard`, `mode`,
`threshold`, `duration_buffer_pct`, `priority_points`) rather than logic
hardcoded into the solver:

- **Hard exclusion** (`mode="hard"`): if a task's defect_type matches a rule
  and the candidate window's forecast trips that hazard (lightning present;
  wind/heat above the rule's threshold), the (task, window) pair is **never
  created** — not deprioritized, structurally absent from the candidate
  pool, for both CP-SAT and the greedy fallback (which consumes the same
  pre-filtered candidate list, so it inherits the exclusion with no separate
  logic).
- **Soft duration buffer** (`mode="soft"`): below the hard threshold, an
  elevated hazard (e.g. >60% rain probability) inflates the task's
  *effective* duration used to size its CP-SAT interval for that specific
  window (duration is computed **per (task, window)**, since two candidate
  windows can carry different days' forecasts) — both the base and
  weather-adjusted duration are persisted to `TaskWeatherAssessment` for
  audit, and the assigned block's real span always equals the effective
  duration used, so metrics/utilization never silently disagree with what
  was actually scheduled.
- **Horizon-aware strictness**: hard exclusion only applies to a candidate
  window within `RELIABLE_HORIZON_DAYS` (7) of today — beyond that, the
  identical hazard is soft/priority-only, never a hard exclusion. Because the
  weekly horizon *is* 7 days, this single date check automatically gives
  weekly both hard+soft treatment on every candidate, and monthly hard+soft
  only for its first ~7 days, soft/priority-only for the rest — exactly the
  distinction the data policy requires, without a second, separately-tracked
  "horizon strictness" flag to keep in sync.
- **Unscheduled reason, explicit and distinct**: a task with zero eligible
  candidates *because* every one was weather-hard-excluded gets a specific
  reason (*"No weather-safe corridor slot found: lightning risk forecast for
  the 2026-09-12 window on GZB-SBB (lightning risk forecast)."*) instead of
  the generic "corridor unavailability" text — shown with a `WEATHER` badge
  in the Control Office's unscheduled-tasks table, not folded in.

> **Verified real example** (weekly horizon, real GZB-SBB lightning
> forecast): a TD `ohe_insulator_fault` task, with its only real
> timetable-safe candidate window falling on 2026-09-12 (real lightning
> risk = true that day), ran through the full weekly CP-SAT solve and came
> back **unscheduled** with exactly the reason above — not silently dropped,
> not scheduled into the unsafe window.
>
> **Verified real example** (admin lever changes the outcome): raising
> `soft-rain-track`'s `duration_buffer_pct` from 20% to 50% via
> `POST /api/admin/weather-rules`, then re-running the weekly scheduler,
> changed a real rail task's effective duration from 2.4h to 3.0h — which no
> longer fit its previous 2.85h window, and the re-solve placed it in a
> different (unforecasted, unbuffered) window instead. Reverted after
> verification.
>
> **Solve time, weather constraints active**: weekly **22.1s** wall-clock,
> monthly **22.3s** — both comfortably under the 30s budget (§6's existing
> 20s `SOLVE_TIME_LIMIT_SECONDS` + build/materialize overhead is unchanged;
> weather assessment itself is one batched query plus dict lookups, not a
> per-candidate DB round trip — same caching discipline as `get_safe_slots`,
> §6's own documented performance fix).
>
> **Monthly horizon does NOT hard-exclude on a >7-day forecast** — verified
> directly against the constraint logic (not just output): a controlled test
> inserted an identical lightning-risk forecast at day+3 and day+12 for the
> same task/corridor; `assess_candidates()` excluded the day+3 candidate and
> kept the day+12 candidate eligible, exactly matching `RELIABLE_HORIZON_DAYS
> = 7`.

Default rules ship seeded (`weather_service.DEFAULT_RULES`, 15 rows spanning
hard/soft/priority) — not hardcoded into the scheduler — covering OHE/
traction (lightning, wind), track/ballast (heat, rain), signal (lightning,
rain, fog), and drainage/embankment/corrosion-prone-OHE (priority). An
Administrator tunes or adds rules the same way scoring weights are tuned:
`GET/POST /api/admin/weather-rules`, `DELETE /api/admin/weather-rules/{id}`
(deactivate) — changing a rule immediately rescores every task (priority
rules) and takes effect on the next scheduler run (hard/soft rules, since
candidate generation happens at solve time).

### 9e. UI (Layer 6)

- **Weather overlay on the Corridor Timeline**: a thin colored band directly
  above each day's track — grey (clear), amber (elevated: soft-buffer
  territory), or red (severe: hard-exclusion territory) — with rain%, wind,
  lightning, fog, and temperature at a glance, and the full forecast
  (including `source`/`provider` and, for a forecast beyond the reliable
  horizon, an explicit "advisory only" note) on hover. It reads from `GET
  /api/weather/forecast`, the exact same `WeatherForecastEntry` rows the
  optimizer itself queries — verified with real data on GZB-SBB: 5 of 7 days
  rendered `wx-severe` (red), matching the real lightning forecast exactly.
  A synthetic-labelled row renders a `SYNTHETIC` badge on its strip, the same
  labelling convention as the demo-task/ML-training-data badges elsewhere.
- **Unscheduled-tasks table**: a weather-caused exclusion shows a distinct
  red `WEATHER` badge ahead of the specific reason text, never folded into
  the generic "lower priority" row.
- **Admin → Weather Sensitivity & Thresholds**: add/edit/deactivate rules
  (pattern, hazard, mode, threshold, buffer%, priority points) with the same
  form+table pattern as the Asset Criticality registry; `w_weather_risk`
  appears in the existing Scoring Weights form since it's just another entry
  in the same admin-configurable weight table.
- COA gets a **"Refresh Weather Forecast"** button next to Render Timeline
  (`POST /api/weather/ingest`), reporting which provider was actually used
  and flagging it when the real adapter was unreachable and the synthetic
  fallback had to be used instead.

---

## 10. Time zone handling — store UTC, display IST — and corridor search

ABPS is an Indian Railways system. Every timestamp a human sees must be
**IST (UTC+5:30)** — a fixed offset, with **no daylight saving**, so
converting to/from it is an exact constant shift, never a calendar-dependent
lookup. This was a systemic, pre-existing correctness bug fixed across the
**entire** codebase (not just new code): every naive `datetime.now()`/
`datetime.utcnow()`/`date.today()` call, every stored timestamp, and every
frontend rendering path.

### 10a. The architecture: store UTC, display IST

- **Storage stays UTC.** `backend/tz_utils.py` is the single source of
  truth. `utc_now()` replaces every ambiguous `datetime.utcnow()`/
  `datetime.now()` call; `ist_today()` replaces every `date.today()` used
  for a "this week"/horizon-start computation (IST business meaning, not
  server-local or UTC). SQLite — this project's local-dev default —
  silently strips tzinfo from *any* `DateTime` column regardless of whether
  it's declared `timezone=True` (confirmed by direct round-trip testing), so
  rather than fight the driver, every naive datetime that flows through
  internal application code is, **by uniform contract**, always UTC;
  `ensure_utc()`/`to_ist()` are the only places a real tzinfo-aware datetime
  is ever constructed — immediately before a value is serialized into an API
  response or shown to a human.
- **API responses always carry an explicit UTC offset** (e.g.
  `"2026-09-12T13:32:55+00:00"`, never a bare ambiguous string) — the
  architecturally cleaner of the two documented options (return raw UTC and
  let the frontend convert, vs. pre-localizing every response to IST),
  because it keeps the backend timezone-agnostic. Enforced **globally**, not
  per-route: `tz_utils.install_global_json_encoder()` (called once in
  `main.py`) patches FastAPI's `datetime` JSON encoder for the many routes
  that return a plain dict; Pydantic `response_model` routes (`TaskOut`,
  `TokenResponse`) use a `BeforeValidator` (`UtcOut`, in `schemas.py`)
  since Pydantic v2's own serializer bypasses FastAPI's encoder entirely —
  verified this actually differs by direct testing before choosing the
  two-mechanism approach.
- **User-typed times are IST.** Every request schema field a user types a
  time into (`CorridorAvailabilityCreate.start_time/end_time`,
  `FreightForecastCreate` windows, `RescheduleRequest.new_start`) uses the
  `IstIn` `BeforeValidator` (`tz_utils.parse_user_local_datetime`): a naive
  submitted value (exactly what an `<input type=datetime-local>` sends) is
  interpreted as IST and converted to UTC for storage; an explicit-offset
  value is honored as given, never reinterpreted.
- **Real train times were a genuine, serious latent bug**, not just a
  display issue. The published Indian Railways timetable times in the real
  dataset (§2) are IST wall-clock times — `timetable_loader.py` was storing
  them as if they were already UTC, with no conversion. `corridor_
  occurrences()` (which expands each train's daily-recurring segment across
  a horizon) now explicitly builds each occurrence in IST first — combining
  the train's real IST time-of-day with the correct **IST calendar day**
  being expanded — before converting to UTC for storage/comparison; this is
  exactly what makes the real-train-safety guarantee (§6) correct at all,
  and exactly the case a near-midnight train would get wrong otherwise (see
  10c #2). The timetable was reloaded fresh (`POST /api/data/load-
  timetable`) so every already-stored `TrainTimetableEntry` picked up the
  fix — this is a data-correction reload, not merely a code change.
- **A real latent bug the stricter output validator surfaced**: `auth.py`'s
  JWT issuance mutates its own payload dict in place when signing (python-
  jose converts `datetime` claims to raw POSIX-timestamp ints *on the same
  dict*), so reading `payload["exp"]` back out *after* `jwt.encode()`
  silently returned an `int`, not the datetime the function's own docstring
  promised — papered over for months by Pydantic's implicit int→datetime
  coercion on the old plain `dt.datetime` schema field. The new `UtcOut`
  validator is stricter (it calls `.tzinfo` on the value) and surfaced this
  immediately as a 500 on login; fixed by capturing `exp` *before* the
  mutating `encode()` call, not re-reading the dict afterward.
- **Horizon/date-range math is IST-calendar-day-based.** `scheduler.py`'s
  `horizon_start` (three call sites) and the CP-SAT `epoch` it builds now
  use `ist_today()`/`ist_date_to_utc_bounds()` instead of `dt.date.today()`
  / naive local midnight — "this week" for an Indian railway corridor means
  the **IST** week, and because IST has no DST, `end = begin + N days` is
  always an exact shift, no timezone-database lookup needed. The same fix
  applies to `derive_gaps()`'s horizon boundaries, the SARIMA traffic
  forecaster's history/forecast windows (`ml/traffic_forecast.py`), and
  every weather forecast date-bucket lookup (`weather_service.py`,
  keyed by IST calendar date via `forecast_date_ist()`, since
  `WeatherForecastEntry.forecast_date` is stored as the UTC-equivalent of
  IST midnight — taking `.date()` on the raw stored value directly would
  silently pick the wrong calendar day).
- **Frontend never uses the browser's own timezone.** `frontend/index.html`
  never calls `toLocaleString()`/`toLocaleDateString()`/`Intl.DateTimeFormat`
  — grepped and confirmed zero matches outside of comments explaining why.
  `toIST()`/`istDateStr()`/`istTimeStr()` apply the identical fixed +5:30
  shift the backend uses (a deliberate "shift the instant, then always read
  via the UTC-suffixed accessors" trick — not a real timezone-database
  conversion, so it can never depend on the viewing browser's ICU data or
  locale). `fmtDt()` — the one function nearly every timestamp display in
  the app goes through — fixes task lists, plan views, the audit log, and
  the timeline detail panel all at once, and labels "IST" explicitly
  wherever a raw timestamp appears (table headers say "(IST)" once rather
  than repeating it per cell where that reads cleaner). The corridor
  timeline's "now" marker, day-matching logic, and the manual-drag
  reschedule feature (which sends a real, correctly-computed UTC instant on
  drop) were all rebuilt on these same primitives — not left as a special
  case.
- **New: "Add Manual Corridor Slot" form** (Control Office view) — the
  `POST /api/corridor-availability` endpoint existed already but had no UI;
  added specifically to exercise and demonstrate the `datetime-local`
  round-trip (§10c #5). What a user types is sent to the backend completely
  unmodified (no client-side `new Date()` reinterpretation, which would
  silently corrupt it through the browser's own timezone) — the backend's
  `IstIn` validator is what performs the actual, correct conversion.

### 10b. Corridor-range search — `GET /api/corridor-search`

Read-only: picks a FROM/TO station pair and an IST date, and returns every
real train moving through that corridor (either direction) plus the weather
forecast, existing corridor-availability windows, and draft/published block
context — **without touching scheduling, the optimizer, or governance**.
Reachable by **every role** via a sidebar button (not one department's
view), opening a modal rather than living inside any one role's dashboard.

- **Station resolution** is fuzzy: exact station-code match wins, otherwise
  a case-insensitive substring match on station name (`corridor_search.
  _resolve_station`).
- **Corridor resolution** is exact when a direct real single-hop segment
  exists between the two stations (this dataset's `corridor_id` is, by
  construction, an adjacent-stop pair — see §2) in either direction. When
  none exists, the busiest real corridor touching one of the requested
  stations is returned instead, explicitly flagged
  `"corridor_match": "approximate"` with a `match_note` explaining exactly
  why and what was substituted — never silently presented as the requested
  corridor.
- `date` is interpreted as an **IST calendar date** — defaults to today IN
  IST (`ist_today()`), not the server's local/UTC date, which can genuinely
  differ near midnight.
- Trains are expanded onto that exact IST date via the same IST-day
  expansion logic `corridor_occurrences()` uses (`timetable_loader.
  train_occurrences_on_date`), so this feature can never disagree with the
  optimizer's own notion of "which trains run on this day."
- The response returns every timestamp as an IST-localized ISO string
  (`*_ist` fields, via `tz_utils.ist_iso()`) — a deliberate, documented
  exception to the "APIs return UTC" convention above, since this endpoint's
  whole purpose is human-facing display, not further backend processing.
- A cheap, read-only `corridor_search` audit-log entry is written per
  search (no scheduling/task/governance state is touched) — allowed
  explicitly by the feature's own read-only scope.

### 10c. Verification — real values, this build

1. **Real train, side-by-side UTC/IST proof**: train **12141** ("MUMBAI LTT
   - PATLIPUTRA SF Exp"), real published departure at station **THK** per
   the source dataset: **00:15:00**. Raw stored DB value (naive, UTC by
   contract): `1999-12-31 18:45:00`. API response: `1999-12-31T18:45:00+00:00`
   (explicit offset). Converted to IST: **00:15:00 IST** — exactly the real
   published time, confirming the +5:30 conversion is exact, not off by an
   hour or using server-local time.
2. **Near-midnight grouping**: requested IST calendar date `2026-09-14` for
   corridor THK-KYN; train 12141's occurrence (raw UTC `2026-09-13
   18:45:00`) converts to IST `2026-09-14 00:15:00+05:30` — correctly
   grouped under the **requested** IST day, not the UTC day the raw value's
   date component would naively suggest.
3. **Horizon boundary, exact**: weekly horizon for `horizon_start =
   2026-09-12` computes `begin = 2026-09-11 18:30:00 UTC` (= `2026-09-12
   00:00:00 IST` exactly) and `end = 2026-09-18 18:30:00 UTC` (= `2026-09-19
   00:00:00 IST` exactly) — a genuine 7×24h IST-calendar-day span, confirmed
   directly against the constraint-building code, not just output.
4. **Timeline "now" marker**: renders correctly against
   `istDateStr(now)`/`_tlMinutesOfDay(now)` — verified live (Playwright): 1
   marker present on the correct IST day, topbar clock reading `2026-09-12
   19:36:29 IST` at the same moment.
5. **datetime-local round-trip, DB-inspected**: typed `2026-09-20T22:15` →
   `2026-09-21T00:15` into the new manual-slot form. Raw stored DB value:
   `2026-09-20 16:45:00` → `2026-09-20 18:45:00` (exactly −5:30, correctly
   crossing UTC midnight for the end time). Displayed back: `2026-09-20
   22:15 IST → 2026-09-21 00:15 IST` — an exact match to what was typed.
6. **Audit log**: column header reads **"Time (IST)"**; a login recorded at
   the same instant as check #4 above shows `2026-09-12 19:33` — consistent
   with the topbar clock.
7. **Corridor search, real data**: `THK → KYN`, `2026-09-12` — resolved
   THK→"THAKRULI", KYN→"KALYAN JN", corridor `THK-KYN` (exact match), **239
   real trains** found for the date (e.g. train 51027 "MumbaiCST-Pandharpur
   Fast Passenger", departs THK **23:47 IST**, arrives KYN **23:50 IST**),
   plus real 7-day rain forecast (89–100% probability) for the corridor.
8. **Full sweep, frontend**: every timestamp display point — topbar clock,
   corridor timeline (day labels, detail panel, drag preview/status, block
   tooltips), before/after comparison timeline, task/plan tables, freight
   forecast, audit log, users table, corridor search (weather/trains/
   availability/blocks) — goes through `fmtDt()`/`istDateStr()`/
   `istTimeStr()` and is explicitly labeled "IST", either per-value or once
   in the column header. None found unconverted.
9. **No browser-local-timezone or naive-`datetime.now()` code paths
   remain**: `grep`-verified. Backend: zero `datetime.utcnow()`/
   `datetime.now()`/`date.today()` calls outside `tz_utils.py` itself (the
   one place they define the canonical replacements). Frontend: zero
   `toLocaleString`/`toLocaleDateString`/`toLocaleTimeString`/
   `Intl.DateTimeFormat` calls anywhere except comments explaining why they
   were deliberately avoided. Two further **real bugs** were found and
   fixed during this sweep, not just the obvious call sites: a
   pre-stringified `.isoformat()` call on a naive datetime in the ML
   training report (`ml/risk_model.py`, bypassed the global JSON-encoder
   fix since it was already a plain string by the time it reached the
   response) and the same pattern in two audit-log detail fields and a
   locked-account error message (`routers/auth.py`, `routers/schedule.py`)
   — all fixed to route through `utc_iso()`/`ist_iso()`.

---

## 11. Real foundation: stations → corridors → vacancy → weather

Four interlocking data-foundation fixes. They are presented together
because they depend on each other in exactly this order: you cannot find
real corridor vacancy without real train data on real corridors between
real stations, and you cannot do weather-aware scheduling without weather
for real station coordinates.

**What was actually found before changing anything** (stated plainly,
because two of the four problems turned out to be different from the
initial diagnosis — and a fix aimed at the wrong problem is worse than no
fix):

| Reported | Actually found |
|---|---|
| Weather broken / paywalled / fabricated | Open-Meteo was already wired and reachable, **but** it fabricated synthetic values whenever the provider failed, was daily-granularity only, and had no cache |
| Stations incomplete | 8,990 loaded with 8,697 coordinates and **zero** timetable gaps — but the loader silently **discarded `state`/`address`**, had no `station_type`/division, and was a destructive delete-and-reload rather than an upsert |
| Corridors synthetic (`CORR-01`) | **No `CORR-01` existed anywhere** in code or database; corridor IDs were already real station pairs. The real gap was that there was **no corridor entity at all** — no intermediate stations, geometry, distance, or junction-to-junction routes |
| Vacancy random / manual | Already computed from real timetable gaps — but with **no safety buffer**, no multi-section intersection, and no record of the bracketing trains |

### 11a. Station master (`station_loader.py`)

Merged from real open datasets, idempotent (UPSERT — safe to re-run,
fill-gaps-only so a lower-priority source can never clobber a better one):

1. **datameet/railways** (vendored at `data/raw/stations.json`) — 8,990
   stations with real Point coordinates, state, zone, address. The loader
   now keeps `state`/`address` instead of dropping them.
2. **vstflugel/indian-railway-dataset** — fetched live; 13,147 stations
   with code/name/region, used purely to ADD stations datameet lacks and
   fill blank names/zones. The file is at the repository **root**, not
   under `data/` — verified against the repo's actual tree via the GitHub
   API after a guessed path 404'd.

`station_type` (junction / terminal / halt / cabin / regular) is **derived
from the real station name** by a documented heuristic, because neither
open source carries an explicit type field. It is what corridor derivation
uses to pick natural endpoints.

An unreachable source is **reported with its URL and error** in the load
result, never silently skipped and never replaced with invented stations to
hit a target count.

### 11b. Corridors (`corridor_builder.py`)

Two kinds, in one table, both derived from real geography:

- **`kind='section'`** — one physical hop between adjacent stations,
  exactly as consecutive real timetable stops define it. 16,791 of them.
  Every corridor_id already referenced by tasks, slots and plans stays
  valid.
- **`kind='route'`** — a contiguous CHAIN of sections between two
  significant stations, with ordered intermediate stations, real polyline
  geometry (GeoJSON LineString from actual station coordinates), and
  haversine distance. 154 of them.

The subtle part is **primary adjacency**. A timetable mixes stopping and
express services, so the raw section set contains both atomic hops
(`NDLS→GZB`) and express skips over them (`NDLS→CNB`). Chaining over the
raw set makes almost every station look like a branch point and collapses
every corridor to two sections — which is exactly what the first
implementation produced. A section `A→B` is treated as PRIMARY only when no
station C exists with both `A→C` and `C→B` also sections, i.e. the hop
cannot be decomposed into smaller observed hops. Express skips are
precisely the decomposable ones, so this recovers the underlying physical
line from the timetable alone, with no external track dataset. After the
fix the longest derived route went from 2 sections to **17**.

### 11c. Vacancy (`vacancy.py`)

A vacancy window is a period in which the real timetable shows **no train
occupying ANY section** of the corridor, buffer included:

1. Expand every real movement on every section onto the requested IST dates.
2. Occupy each section from departure to arrival **± a safety buffer**
   (default 15 min — signal clearance). Merge overlaps.
3. Intersect the per-section free intervals — a corridor is vacant only
   when **all** of it is vacant simultaneously.
4. Drop anything shorter than `min_window_hours` (default 1.0).
5. Record the **real bracketing trains** so any window is independently
   checkable against the published timetable.

Uses **only** the static timetable — fully offline, never dependent on a
live API. `POST /api/corridor-availability/derive?corridor_id=&date_from=
&date_to=&buffer_minutes=&min_window_hours=` is re-runnable (replaces
derived windows in that range; manual windows are never touched).

The buffer is *also* re-applied in `scheduler._slot_is_safe`, the single
point every candidate window passes through, so windows this system did not
compute — manual entries, ML-forecast windows, or leftovers from an older
revision — are held to the same margin. 60 stale unbuffered windows from
the previous algorithm were purged during the upgrade.

### 11d. Weather (`adapters/weather.py`, `weather_service.py`)

- **Open-Meteo, hourly** (free, no API key, no signup): temperature,
  precipitation probability, rain, wind, visibility, WMO weather code,
  `timezone=Asia/Kolkata`.
- **Hourly, not daily, is the point.** Measured on this build for Delhi:
  WMO code 95 (thunderstorm) at exactly 04:00–05:00 IST with the rest of
  the day clear. A daily roll-up would have excluded a whole day of usable
  windows. The optimizer's per-window check aggregates exactly the hours a
  candidate window spans; it falls back to the daily roll-up only where no
  hourly data exists.
- **The synthetic weather adapter is deleted.** It previously generated
  plausible values whenever the provider failed. Fabricating weather for a
  system that decides whether it is safe to send a crew up an OHE mast is
  not an acceptable failure mode, however clearly it is labelled. Now:
  provider unreachable → `available: false` with the real error, cached
  rows retained untouched, and the UI says **"weather unavailable — not
  assumed clear"**.
- **Cached** for 6h (`FORECAST_TTL_HOURS`, `force=true` to bypass).
- Fetched for the corridor's real **midpoint** coordinates (a corridor is a
  stretch of line, not a point), from the station master.
- A daily roll-up is still maintained from the hourly rows, so the
  admin-tunable rule table, timeline overlay and priority scoring keep
  working unchanged.

### 11e. Verification — real measured results, this build

1. **Stations**: 13,479 total, **8,697 with coordinates**, 0 timetable
   gaps. Sources reported individually (datameet 8,990; vstflugel 13,147).
   The five named stations: `NDLS` NEW DELHI (28.642314, 77.220004),
   `HWH` HOWRAH JN (22.584078, 88.340999), `MAS` CHENNAI CENTRAL
   (13.084761, 80.274856), `SBC` BANGALORE CITY JN (12.977595, 77.568083),
   and **`MMCT`** — which exists in **neither** open dataset: Mumbai
   Central is carried as **`BCT`** (18.970667, 72.819383) in both. Rather
   than invent a station row, a small documented alias table
   (`STATION_CODE_ALIASES`) maps the familiar IRCTC/NTES code onto the real
   loaded one, so searching "MMCT" resolves correctly.
2. **Completeness**: 10 random timetable codes (`VDN, DSPT, AKI, GEB, MLO,
   SJDR, NNA, BWV, SNTD, ARW`) → **10/10** resolved in the master.
3. **Corridor derivation**: `DURG-MUA`, 6 sections, 47.49 km, 42 trains,
   zone SEC — **DURG → RSM Rasmara → MUP Murhipar → PMS Parmalkasa → RJN
   Raj Nandgaon → BAKL Bakal → MUA Musra**, the real SECR main line west of
   Durg. Durg–Rajnandgaon is ~31 km in reality; the haversine sum to RJN is
   ~30 km, correctly slightly under actual track length.
4. **No synthetic corridor IDs**: 0 across code and all five corridor-
   bearing tables (corridors, corridor_slots, maintenance_tasks,
   block_plan_entries, timetable).
5. **Vacancy, with arithmetic that checks out**: `SBB-GZB`
   (Sahibabad→Ghaziabad, 139 real movements) on 2026-09-14 →
   **02:45 → 04:57 IST (2.2h)**, after train **54308** *Delhi Moradabad
   Passenger* (arrives GZB **02:30** IST, +15 min buffer = 02:45) and
   before train **54539** *Nizamuddin Ambala Passenger* (departs SBB
   **05:12** IST, −15 min buffer = 04:57). Both trains confirmed present in
   the loaded timetable. (The brief's illustrative `NDLS-GZB` is not an
   adjacent-stop pair in this dataset; `SBB-GZB` is the real busiest
   approach to Ghaziabad.) On the 6-section `DURG-MUA`, 3 windows were
   found against 257 movements with all 6 sections required free.
6. **No window overlaps a train**: `verify_no_overlap` — a deliberately
   separate naive scan, not shared code — reports **clean, 0 violations**
   for both corridors. Proven non-vacuous by a **negative control**: a
   window planted on train 12860's real 03:25 IST departure was caught with
   **27 violations** (correctly citing the buffered 03:10–03:45 occupancy),
   and the check returned clean again once removed.
7. **Live weather**: `DURG-MUA` midpoint (21.176646, 81.085605), 168 hourly
   rows. An **independent direct call to Open-Meteo**, bypassing ABPS
   entirely, returned `2026-09-12T00:00 temp=24.2C precip=99% wind=13.8km/h
   vis=2880m code=53` — matching the stored rows **value for value**.
8. **Provider failure**: with the adapter pointed at an unreachable host —
   corridor with no cache → `available: false`, real error surfaced, **0
   rows written**; corridor with cache → **168 cached rows retained**,
   still `available: false`. Only source present in the weather tables is
   `open_meteo`/`real`; `SyntheticWeatherAdapter` no longer exists in the
   codebase.
9. **Weather → optimizer**: on `URR-BMW`, real vacancy gave windows of
   8.00h / 5.77h / 2.95h (all overlapping **real** WMO-95 lightning hours)
   and 4.65h (clear). A **5.5h** TD/OHE task — which only fits the stormy
   windows — came back **UNSCHEDULED**: *"No weather-safe corridor slot
   found: lightning risk forecast for the 2026-09-14 00:00-08:00 IST window
   on URR-BMW (lightning risk forecast, hourly forecast); …"*. The
   counter-test proves this is hour-precise rather than blanket: a **4h**
   task of the same defect type on the same corridor **was scheduled**, at
   **19:21–23:21 IST**, inside the one clear window.
10. **End-to-end from the dashboard**: searching the station **names**
    "Sahibabad" → "Ghaziabad" resolved to `SBB`/`GZB`, corridor `SBB-GZB`,
    and rendered **279 real trains** with real names and service classes
    (*New Delhi-Lucknow AC Duronto Express* `Drnt`, *Lucknow Mail* `SF`,
    *Ambala Delhi Passenger* `Pass`) at real IST times, live Open-Meteo
    weather with lightning flagged, and the computed vacancy window citing
    trains 54308/54539. 0 console errors. A duplicate-weather-day bug
    (both corridor directions carrying a forecast for the same date) was
    caught from the rendered screenshot and fixed.

`service_type` is now populated from the dataset's own real `type` field
(Exp 164,161 segments / SF 119,052 / Pass 52,843 / Raj 10,317 / Drnt 8,182
/ GR 7,555 / MEMU 5,759) — it was stored empty for every segment before.

---

## 11f. Crew & resource capacity (Feature 10)

The optimizer now knows how many gangs exist. `DepartmentCapacity` holds a
standing default per department (seeded ENG 4 / TD 3 / SNT 3) plus optional
per-IST-date overrides ("SNT has 1 gang on 16 Sep — training"). All logic
lives in `backend/crew_capacity.py`.

- **CP-SAT:** one `AddCumulative` per department over every candidate
  interval of that department on **all** corridors (demand 1 = one gang),
  alongside the existing per-corridor cumulative — both must hold. Per-date
  capacities stay inside that single constraint: the ceiling is the highest
  capacity in the horizon, and each lower-capacity IST day carries a fixed
  reservation interval (00:00–24:00 IST) demanding the difference.
- **Hard everywhere.** The greedy fallback, manual assign/swap, drag
  reschedule and the override dry-run all apply the same count check; a
  manual override that would exceed crew is rejected with the reason. A
  pinned override that no longer fits (capacity lowered afterwards) competes
  as a normal task and is listed in `metrics.pinned_overrides_released_for_crew_capacity`.
- **Gang labels** (`GangAssignment`) are assigned post-solve by greedy
  interval colouring (sort by start, lowest free gang) and rebuilt whenever a
  plan's entries change.
- **Distinct reason:** a task left out because no candidate window has a free
  gang gets `reason_code = crew_capacity`, e.g. *"Engineering has 2 gangs
  available; all 2 are already committed during every candidate window for
  this task on this corridor (DKAE-GBRA)."*
- **API:** `GET/POST/PATCH/DELETE /api/admin/department-capacity` (ADMIN
  writes, COA reads, every change audit-logged with before/after),
  `GET /api/admin/department-capacity/impact` (dry run: "reducing ENG to 1
  gang would make 1 currently-scheduled task infeasible"). `GET
  /api/schedule/plan` adds `gang_id` per entry and `resource_summary` per
  department; CSV/PDF export gains a gang column.
- **UI:** Admin capacity table with live impact warnings; Control Office
  crew utilization strip (green < 75 %, amber 75–100 %, red when tasks were
  crew-limited or the plan exceeds a since-lowered capacity) — click a cell to
  see where that department's gangs work on the map; optional "Gang lanes"
  timeline toggle; department desks get a Gang column and per-gang work sheets.

## 11g. Live railway network map (Feature 11)

Sidebar → **Network Map** (every role). Leaflet 1.9.4 from unpkg (SRI-pinned)
with OpenStreetMap tiles — no API key. Backend: `backend/map_service.py`,
`backend/routers/network_map.py`.

- `GET /api/map/corridors?date=&horizon=&include_clear=` and
  `GET /api/map/stations?date=&horizon=&include_quiet=` return GeoJSON
  FeatureCollections (real polylines / real coordinates, status per IST date,
  active block with gang assignments, today_summary / today_activity).
  Detail endpoints `GET /api/map/corridors/{id}` and `/api/map/stations/{code}`
  feed the popups (blocks, trains with next 3 IST departures, weather — the
  ingested forecast if present, else a live read-only Open-Meteo lookup that
  is never written to the DB; "unavailable" if the provider can't be reached).
- Static geometry is parsed once and cached in memory; clear features are
  pre-serialized and spliced into responses; responses are gzip-compressed.
- Station "major" tier = junction/terminal **or** ≥ 20 real trains starting/
  ending there (the name heuristic types New Delhi, Chennai Central and
  Mumbai CST as `regular`). `MMCT` resolves to the dataset's `BCT`.
- Zoom 5–7: major stations + active/upcoming corridors only; 8–10: regular
  stations + the full section network in view; 11+: halts. Both directions of
  a section share geometry, so clear lines are de-duplicated per station pair
  and status corridors are kept on top for hit-testing.
- 60 s auto-refresh (configurable) polls only corridors/stations with blocks;
  a status change flashes the line. Department roles see other departments'
  blocks as department + times only (task/gang details redacted).

**Independence.** The map imports `crew_capacity` optionally (no gang labels
without it — verified by building the map with the module disabled). Removing
the map means deleting `map_service.py`, `routers/network_map.py` and their
lines in `main.py`; the scheduler never imports map code. In the frontend,
the strip → map click and the timeline "Show on map" button only activate
when the map view exists.

## 11h. Features 10 & 11 — verification (real measured results, this build)

Run against a **copy** of the database through the real HTTP API and a real
browser (Playwright/Chromium, browser timezone deliberately set to
America/New_York to prove IST display doesn't depend on it). Test windows are
**real vacancy** computed from the timetable (Eastern Railway night gap,
~00:25–06:05 IST), so every block also passes the train-safety filter.

| # | Check | Result |
|---|---|---|
| 1 | ENG = 2 gangs, 5 ENG tasks (4.5 h) competing for 14 Sep night windows on DKAE-GBRA / JOX-BPAE / BRPA-MBE | Scheduled: DKAE-GBRA 00:23–04:53 IST **ENG-GANG-1**, JOX-BPAE 00:27–04:57 IST **ENG-GANG-2**. Independent sweep: max 2 simultaneous. Controls: without the crew constraint CP-SAT runs all 5 at once; greedy fallback with it also stops at 2. |
| 2 | Crew-limited reason | *"Engineering has 2 gangs available; all 2 are already committed during every candidate window for this task on this corridor (DKAE-GBRA)."* (`reason_code: crew_capacity`; those corridors held 0–1 of 3 tasks). A manual-assign into the same window → HTTP 400 with the crew reason. |
| 3 | SNT = 1 on 16 Sep, set through the Admin UI, scheduler re-run | 16 Sep: max **1** simultaneous SNT; 15 Sep (default 3): **3** simultaneous. Fifth task's reason cites *"Date overrides in effect: 16 Sep = 1 gang (…training)"*. |
| 4 | 3 sequential TD tasks (HIH-GRAE, 17/18/19 Sep 00:54–05:54) | All **TD-GANG-1**. Unit checks: 3 back-to-back → 1 gang; 4 parallel → GANG-1..4; A(1–3) B(2–5) C(3–4) → A,C GANG-1, B GANG-2. |
| 5 | Audit | e.g. `2026-09-13 07:23:01 IST user=admin department_capacity_updated` before 4 → after 2; `department_capacity_created` SNT 2026-09-16 = 1. |
| 6 | Station positions (zoom-5 markers vs reference) | NDLS 28.6423, 77.2200 · BCT/MMCT 18.9707, 72.8194 · HWH 22.5841, 88.3410 · MAS 13.0848, 80.2749 · SBC 12.9776, 77.5681 — all within **0.10–0.23 km**. |
| 7 | Polylines follow stations | Every vertex equals its station's coordinates, in order (DC-CV: DC → MVPM → SGE → ANU → CV; JKE-PKRD; DKAE-GBRA). |
| 8 | Visible markers by zoom (Kolkata) | z5 **472** (major only) · z7 226 · z8 1,582 · z9 652 · z10 330 · z11 207 (halts appear) · z12 110. Redraw ≤ 15 ms. |
| 9 | Click active corridor JKE-PKRD | Popup: ACTIVE, *"ENG-GANG-1 — rail_crack_repair (ENG), 07:04–09:30 IST"*, TD-GANG-1, weather; "View timeline" opened the JKE-PKRD timeline. |
| 10 | Click NDLS | 229 real trains; next departures 07:40 IST 12011 Kalka Shatabdi, 08:10 18508, 08:30 54474; live weather incl. thunderstorm 12:00/15:00 IST. |
| 11 | Auto-refresh | "Last updated" advanced every 30 s; a block turned upcoming → active at 07:36 IST and its line flashed. |
| 12 | Timeline "Show on map" | Map opened at zoom 13 with JKE-PKRD fully in view. |
| 13 | Performance | `/api/map/corridors` ~90 ms (16,394 features, gzipped), `/api/map/stations` ~50 ms (8,697). Click → drawn 490–532 ms. Zoom 5: **60 fps** idle, 60 fps dragging, 58–60 fps wheel-zooming (38–44 fps while dragging with a cold tile cache right after first load). |

Bugs these checks caught and fixed: the clear reverse-direction section
(PKRD-JKE shares JKE-PKRD's geometry) stole clicks from the active corridor;
a still-loading Control Office view could overwrite the map after navigating
away; the Admin impact warning counted entries of cancelled tasks.

Known limitations: gangs are assumed free to move between corridors between
tasks (no travel time is modelled); the open dataset has no day-of-week
running data, so "trains today" treats every timetable segment as daily.

---

## 12. Project structure

```
backend/
  main.py              FastAPI app, startup wiring, serves frontend at /
  database.py           SQLAlchemy engine/session (SQLite; swap via ABPS_DATABASE_URL)
  models.py              ORM models (all 6 layers, incl. department_capacity + gang_assignments)
  schemas.py              Pydantic request/response schemas
  priority_engine.py       Rule-based scorer + configurable scoring source (rule/ml/blend)
  scheduler.py               CP-SAT interval optimizer + greedy fallback + simulate_schedule()
  crew_capacity.py            §11f: gang capacity model, crew cumulative helpers, gang labelling, impact analysis
  map_service.py               §11g: network map GeoJSON (cached geometry, per-date block status, popups)
  decision_intelligence.py     Layer 5: emergency re-opt, what-if, explain, shadow prices
  weather_service.py            §9/§11d: hourly Open-Meteo ingestion, cache, rules, optimizer feasibility
  corridor_search.py             §10b: read-only FROM/TO station corridor search
  tz_utils.py                     §10a: single source of truth for UTC storage / IST display
  station_loader.py                §11a: real station master, multi-source idempotent merge
  corridor_builder.py               §11b: real corridors from railway geography (sections + routes)
  vacancy.py                         §11c: real corridor vacancy from actual train movements
  baseline.py                    Manual-process "before" simulation
  metrics.py                       Shared before/after metrics (used by both)
  timetable_loader.py                Real data.gov.in/DataMeet ingestion + gap derivation
  auth.py                              Password hashing, JWT issuance/verification, RBAC dependencies
  seed.py                                Baseline users (hashed passwords) + optional demo fixtures
  audit.py                                Append-only audit log helper
  adapters/                         Pluggable TMS/SMMS/TDMS/COA/weather adapter interfaces + stubs
  importers/                         CSV/Excel ingestion + validation + templates
  pipeline/                           Layer 2: cleaning.py, normalization.py, id_mapping.py
  ml/                                   Layer 3: synthetic_data.py, features.py, risk_model.py, traffic_forecast.py
  routers/                               auth, tasks, corridor, schedule, admin, pipeline, ml, decision, weather, network_map
migrations/            Alembic migrations (backend/database.Base.metadata is the source of truth)
frontend/
  index.html            Single-page, role-adaptive dashboard + Leaflet network map (CDN, no build step)
data/
  raw/                 Real downloaded timetable/station/train JSON
  abps.db              SQLite database (created on first run)
  ml_models/           Persisted trained model artifacts (joblib)
docker-compose.yml    Optional PostgreSQL (postgres:16-alpine)
requirements.txt
run.sh
```

## 13. API reference

```
POST       /api/auth/login                      {username, password} -> access+refresh tokens
POST       /api/auth/refresh                    {refresh_token} -> new (rotated) token pair
POST       /api/auth/logout                     {refresh_token} -> revokes it
GET        /api/auth/me                         current identity
POST       /api/auth/change-password            {current_password, new_password}
POST       /api/auth/impersonate                {target_user_id} — ADMIN only, see §1c

POST/GET   /api/tasks                          submit & list tasks (row-scoped for ENG/TD/SNT)
POST       /api/tasks/import                    CSV/Excel bulk import, ?department=ENG|TD|SNT
GET        /api/tasks/template?department=      download import template (public)
POST       /api/tasks/{id}/flag-safety          elevate priority via safety flag
POST       /api/tasks/{id}/flag-interlocking    S&T only
POST       /api/tasks/{id}/mutual-exclusion     {other_task_id}
GET/POST   /api/corridor-availability            COA defines/queries windows
POST       /api/corridor-availability/derive      compute real traffic-gap windows
GET        /api/timetable, /api/timetable/top-corridors, /api/stations, /api/freight-forecast
POST       /api/data/load-timetable              ingest the real timetable + merge the station master (§11a)
POST       /api/data/load-stations?include_remote=  §11a: merge the real station master (idempotent upsert)
POST       /api/corridors/build                    §11b: derive real corridors (sections + junction routes)
GET        /api/corridors?kind=&q=&min_trains=     list derived corridors
GET        /api/corridors/{corridor_id}             one corridor: stations, geometry, distance, sections
POST       /api/corridor-availability/derive?corridor_id=&date_from=&date_to=&buffer_minutes=&min_window_hours=
                                                    §11c: real vacancy from actual train movements
GET        /api/corridor-availability/verify?corridor_id=&date_from=&date_to=
                                                    independent no-overlap check (separate naive scan)
GET        /api/weather/hourly?corridor_id=&hours=  §11d: hourly Open-Meteo rows the optimizer reads
POST       /api/schedule/run?horizon=weekly|monthly&corridor_capacity=3
GET        /api/schedule/plan?horizon=            plan + unscheduled list (row-scoped); gang_id per entry,
                                                    reason_code per unscheduled task, resource_summary per department
POST       /api/schedule/entries/{id}/reschedule  {new_start} — COA, draft plans only, re-validated (FR-COA-03)
GET        /api/schedule/plan/export?horizon=&format=csv|pdf   FR-DASH-06, row-scoped
POST       /api/schedule/approve                  {plan_id, decision: approve|reject}
GET        /api/schedule/metrics?horizon=
GET        /api/schedule/comparison?horizon=      baseline (manual) vs optimized, with delta
GET/POST   /api/admin/users, /api/admin/scoring-config, /api/admin/asset-criticality
GET/POST   /api/admin/scoring-source             rule|ml|blend — ADMIN changes, COA read-only
GET        /api/admin/audit-log                  ADMIN and COA (see §1c)

GET        /api/network-map?horizon=             busiest-corridor activity table (Control Office panel)
GET        /api/corridor-search?from_station=&to_station=&date=   read-only §10b, every role, IST date

# Layer 2 -- data pipeline
GET        /api/pipeline/batches                 ingestion batch stats, row-scoped by department
GET        /api/pipeline/review-queue            unresolved rows awaiting a mapping (see Layer 2 section)
POST       /api/pipeline/review-queue/{id}/resolve   COA/ADMIN: registers the missing mapping
GET/POST   /api/pipeline/asset-mappings          source-system asset_id -> canonical (COA/ADMIN writes)
GET/POST   /api/pipeline/corridor-mappings       source-system corridor_id -> canonical (COA/ADMIN writes)

# Layer 3 -- AI / Analytics
POST       /api/ml/train?n_records=2000          trains XGBoost + Random Forest on synthetic data (COA/ADMIN)
GET        /api/ml/evaluation                    latest training report: metrics, feature importances, limitations
GET        /api/ml/predict/{task_id}             live risk prediction + SHAP explanation for one task
GET        /api/ml/traffic-forecast?corridor_id=&horizon=   real-data SARIMA low-traffic windows

# Layer 5 -- Decision Intelligence (all COA)
POST       /api/decision/emergency-reoptimize?horizon=   inject an urgent defect, re-solve with a deviation penalty
POST       /api/decision/what-if?horizon=                 compare N read-only scenario configs side by side
GET        /api/decision/explain/{task_id}?horizon=        why scheduled / counterfactual + named competitor
GET        /api/decision/shadow-price?corridor_id=&extra_hours=   marginal value of more corridor time

# §9 -- Weather-aware scheduling
GET        /api/weather/forecast?corridor_id=&horizon=   forecast rows the optimizer/timeline both read
POST       /api/weather/ingest                            {corridor_id, days} — COA, fetches real (or labelled-synthetic-fallback) data
GET        /api/weather/health                            live connectivity of the real + synthetic adapters
GET        /api/weather/assessments?plan_id=               per-task weather audit: base vs. effective duration, hard/soft outcome
GET/POST   /api/admin/weather-rules, DELETE .../{rule_id}  hard/soft/priority sensitivity rules — ADMIN writes, COA read-only

# §11f -- Crew & resource capacity
GET        /api/admin/department-capacity                 defaults + date overrides (ADMIN, COA)
POST       /api/admin/department-capacity                 {department, date?, max_concurrent_gangs, notes} — ADMIN, audited
PATCH      /api/admin/department-capacity/{id}            {date?, max_concurrent_gangs?, notes?} — ADMIN, audited
DELETE     /api/admin/department-capacity/{id}            date overrides only (defaults are edited, not deleted) — ADMIN, audited
GET        /api/admin/department-capacity/impact?department=&max_concurrent_gangs=&date=&replacing_id=
                                                          dry run: tasks a proposed capacity would make infeasible

# §11g -- Live railway network map (every role)
GET        /api/map/corridors?date=&horizon=&include_clear=&bbox=   GeoJSON corridors with per-date status + gangs
GET        /api/map/stations?date=&horizon=&include_quiet=&bbox=    GeoJSON stations with tier + today_activity
GET        /api/map/corridors/{corridor_id}?date=&horizon=          popup: stations, distance, blocks, weather
GET        /api/map/stations/{station_code}?date=&horizon=          popup: trains (next 3 IST), blocks, weather
```

Every route above except `/api/auth/login`, `/api/auth/refresh`, and `GET
/api/tasks/template` requires `Authorization: Bearer <access_token>`. See
§1c for the exact role permitted on each route, and §1b for how the token
is issued and verified. `plan_id`/`decision`/`user_id` are no longer
accepted as trusted client fields anywhere — the acting identity always
comes from the token.
