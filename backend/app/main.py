from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import models  # noqa: F401  (registers tables on Base.metadata)
from .config import settings
from .database import Base, engine
from .routers import capacity, collectors, dashboard, integrations, inventory, topology


@asynccontextmanager
async def lifespan(_: FastAPI):
    # MVP: create tables on startup. Switch to Alembic migrations before production data lands.
    Base.metadata.create_all(engine)
    yield


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

for r in (inventory.router, integrations.router, collectors.router, topology.router, capacity.router, dashboard.router):
    app.include_router(r, prefix="/api")


@app.get("/api/health", tags=["System"])
def health():
    return {"status": "ok"}
