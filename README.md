# ABPS — Automatic Block Planning System

An AI-assisted platform that coordinates maintenance corridor "block"
scheduling across three Indian Railways departments — Engineering (ENG),
Traction Distribution (TD), and Signal & Telecom (SNT) — around a Control
Office (COA) view of real train traffic. It scores every maintenance task by
urgency/risk with a transparent formula, then uses Google OR-Tools CP-SAT to
pack compatible tasks from *different* departments into the *same* corridor
closure wherever possible, so the corridor shuts once instead of three times.

Nothing is ever scheduled outside human control: the optimizer only ever
produces a **draft** plan. It becomes real only when the Control Office
explicitly approves it.

---

## 1. Quick start

### Requirements
- Python 3.10+ (tested on 3.14)
- ~150 MB free disk (the real train-timetable dataset)
- Internet access the first time you load the timetable (or pre-fetch the
  files per §2 below and run fully offline afterwards)

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
set ABPS_SECRET_KEY=pick-a-long-random-value-and-keep-it-fixed
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
| `POST /api/corridor-availability`, `/derive` | **COA only** |
| `GET /api/freight-forecast` | any authenticated user |
| `POST /api/freight-forecast` | **COA only** |
| `POST /api/schedule/run` | **COA only** |
| `GET /api/schedule/plan`, `/metrics`, `/comparison` | any authenticated user; task-level rows row-scoped for ENG/TD/SNT |
| `POST /api/schedule/approve` | **COA only** |
| `GET/POST /api/admin/*` (users, scoring-config, asset-criticality) | **ADMIN only** |
| `GET /api/admin/audit-log` | **ADMIN and COA** — the one deliberate exception to the blanket admin-only rule above; Control Office needs operational audit visibility without full system-config rights |

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

## 6. The optimizer

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

## 7. Governance

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

## 9. Project structure

```
backend/
  main.py              FastAPI app, startup wiring, serves frontend at /
  database.py           SQLAlchemy engine/session (SQLite; swap via ABPS_DATABASE_URL)
  models.py              ORM models
  schemas.py              Pydantic request/response schemas
  priority_engine.py       Transparent weighted scorer
  scheduler.py               CP-SAT interval optimizer + greedy fallback
  baseline.py                  Manual-process "before" simulation
  metrics.py                     Shared before/after metrics (used by both)
  timetable_loader.py              Real data.gov.in/DataMeet ingestion + gap derivation
  auth.py                            Password hashing, JWT issuance/verification, RBAC dependencies
  seed.py                              Baseline users (with hashed passwords) + optional demo fixtures
  audit.py                              Append-only audit log helper
  adapters/                       Pluggable TMS/SMMS/TDMS/COA adapter interface + stubs
  importers/                       CSV/Excel import + templates
  routers/                          auth.py, tasks.py, corridor.py, schedule.py, admin.py
frontend/
  index.html            Single-page, role-adaptive dashboard (no build step)
data/
  raw/                 Real downloaded timetable/station/train JSON
  abps.db              SQLite database (created on first run)
requirements.txt
run.sh
```

## 10. API reference

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
POST       /api/data/load-timetable              fetch/ingest the real data.gov.in timetable dataset
POST       /api/schedule/run?horizon=weekly|monthly&corridor_capacity=3
GET        /api/schedule/plan?horizon=            plan + unscheduled list (row-scoped)
POST       /api/schedule/entries/{id}/reschedule  {new_start} — COA, draft plans only, re-validated (FR-COA-03)
GET        /api/schedule/plan/export?horizon=&format=csv|pdf   FR-DASH-06, row-scoped
POST       /api/schedule/approve                  {plan_id, decision: approve|reject}
GET        /api/schedule/metrics?horizon=
GET        /api/schedule/comparison?horizon=      baseline (manual) vs optimized, with delta
GET/POST   /api/admin/users, /api/admin/scoring-config, /api/admin/asset-criticality
GET        /api/admin/audit-log                  ADMIN and COA (see §1c)
```

Every route above except `/api/auth/login`, `/api/auth/refresh`, and `GET
/api/tasks/template` requires `Authorization: Bearer <access_token>`. See
§1c for the exact role permitted on each route, and §1b for how the token
is issued and verified. `plan_id`/`decision`/`user_id` are no longer
accepted as trusted client fields anywhere — the acting identity always
comes from the token.
