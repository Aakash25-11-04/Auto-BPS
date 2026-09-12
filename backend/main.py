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
from fastapi.responses import FileResponse

import seed
from database import Base, SessionLocal, engine
from routers import admin, auth as auth_router, corridor, schedule, tasks

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

DEMO_DATA = os.environ.get("ABPS_DEMO_DATA", "").lower() in ("1", "true", "yes") or "--demo-data" in sys.argv
DEMO_CORRIDOR = os.environ.get("ABPS_DEMO_CORRIDOR", "NDLS-GZB")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed.seed_baseline_users(db)
        if DEMO_DATA:
            seed.seed_demo_tasks(db, DEMO_CORRIDOR)
    finally:
        db.close()
    yield


app = FastAPI(title="Automatic Block Planning System (ABPS)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(tasks.router)
app.include_router(corridor.router)
app.include_router(schedule.router)
app.include_router(admin.router)


@app.get("/health")
def health():
    return {"status": "ok", "demo_data_enabled": DEMO_DATA}


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
