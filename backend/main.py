"""ABPS FastAPI application entry point.

Run directly (`python main.py`) or via `uvicorn main:app`. Demo fixture data
(clearly separated, source='demo') is OFF by default; opt in with either
`python main.py --demo-data` or the ABPS_DEMO_DATA=1 environment variable
(the env var is what to use when launching via `uvicorn main:app`, since
uvicorn's own CLI args replace argv).
"""
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse

import crew_capacity
import map_service
import seed
import tz_utils
from database import Base, DATABASE_URL, SessionLocal, engine
from routers import admin, auth as auth_router, corridor, decision, ml, network_map, pipeline, schedule, tasks, weather

# Must run before any request is served: every plain-dict JSON response in
# the app (the majority of routes) picks up an explicit UTC offset on every
# datetime field from this one call — see tz_utils.install_global_json_encoder.
tz_utils.install_global_json_encoder()

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

DEMO_DATA = os.environ.get("ABPS_DEMO_DATA", "").lower() in ("1", "true", "yes") or "--demo-data" in sys.argv
DEMO_CORRIDOR = os.environ.get("ABPS_DEMO_CORRIDOR", "NDLS-GZB")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # SQLite (the zero-config local-dev default) still gets its schema from
    # create_all() automatically, so `python main.py` keeps working with no
    # extra step. A real deployment (ABPS_DATABASE_URL pointing at Postgres)
    # is expected to own its schema via Alembic instead — run
    # `alembic upgrade head` before starting the app — so create_all() is
    # skipped there rather than fighting Alembic for ownership of the schema.
    if DATABASE_URL.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed.seed_baseline_users(db)
        crew_capacity.ensure_default_capacity(db)  # Feature 10: ENG 4 / TD 3 / SNT 3 unless Admin changed them
        if DEMO_DATA:
            seed.seed_demo_tasks(db, DEMO_CORRIDOR)
    finally:
        db.close()
    map_service.warm_cache(SessionLocal)  # Feature 11: parse map geometry off the request path
    yield


app = FastAPI(title="Automatic Block Planning System (ABPS)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# The network map's FeatureCollections are several MB of highly repetitive
# JSON; gzip cuts them ~5-8x. compresslevel 5 keeps compression itself cheap.
app.add_middleware(GZipMiddleware, minimum_size=2048, compresslevel=5)

app.include_router(auth_router.router)
app.include_router(tasks.router)
app.include_router(corridor.router)
app.include_router(schedule.router)
app.include_router(admin.router)
app.include_router(pipeline.router)
app.include_router(ml.router)
app.include_router(decision.router)
app.include_router(weather.router)
app.include_router(network_map.router)


@app.get("/health")
def health():
    return {"status": "ok", "demo_data_enabled": DEMO_DATA}


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
