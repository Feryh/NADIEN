from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from . import models  # noqa: F401  (registers tables on Base.metadata)
from .collectors.runner import runner
from .collectors.scheduler import Scheduler
from .config import settings
from .database import SessionLocal, init_schema
from .routers import (ai, analysis, auth, capacity, changes, collectors, configs, dashboard, insights, integrations,
                      inventory, mop, paths, portal, sso, topology, twin, validation)
from .services import cache, collector_settings
from .services.auth import authorize, ensure_admin


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Alembic migrations to head (app/migrations); a pre-migration database is stamped at the baseline first.
    init_schema()
    if settings.auth_enabled:
        with SessionLocal() as db:
            ensure_admin(db)
    collector_settings.refresh()  # values saved in Pengaturan Collector override backend/.env
    scheduler = Scheduler(runner) if settings.scheduler_enabled else None
    if scheduler:
        scheduler.start()
    yield
    if scheduler:
        scheduler.stop()


app = FastAPI(
    title="NADIEN",
    description="Network Access Design, Implementation & Engineering Platform",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Topology/capacity payloads are large JSON (thousands of rows); compressing them cuts transfer ~10×.
app.add_middleware(GZipMiddleware, minimum_size=2048)


@app.middleware("http")
async def drop_cache_on_write(request: Request, call_next):
    response = await call_next(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and response.status_code < 400:
        cache.clear()
    return response


for r in (
    inventory.router,
    integrations.router,
    collectors.router,
    collectors.backups,
    analysis.router,
    paths.router,
    topology.router,
    capacity.router,
    dashboard.router,
    insights.router,
    validation.router,
    mop.router,
    configs.router,
    ai.router,
    twin.router,
    changes.router,
    auth.account,
    auth.users,
    auth.audit_logs,
    portal.router,
    sso.settings_router,
):
    app.include_router(r, prefix="/api", dependencies=[Depends(authorize)])
app.include_router(auth.public, prefix="/api")
app.include_router(sso.public, prefix="/api")


@app.get("/api/health", tags=["System"])
def health():
    return {"status": "ok"}
