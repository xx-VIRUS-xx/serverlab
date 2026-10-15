"""FastAPI target: fixed-duration DB work behind a psycopg connection pool.

``GET /work`` holds one pooled connection for ``SERVICE_TIME_MS`` by running
``SELECT pg_sleep(...)``. The pool is the only limiter: ``min_size == max_size ==
POOL_SIZE`` and there is no ``max_waiting`` cap, so excess requests queue until
``POOL_TIMEOUT_S`` expires. Run with exactly one uvicorn worker (see Dockerfile).
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool

DEFAULT_SERVICE_TIME_MS = 50
DEFAULT_POOL_TIMEOUT_S = 30.0
POOL_COUNTERS = (
    "requests_num", "requests_queued", "requests_wait_ms", "requests_wait_max",
    "pool_min", "pool_max", "pool_size", "pool_available", "requests_waiting",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    database_url: str
    pool_size: int
    service_time_ms: int = DEFAULT_SERVICE_TIME_MS
    pool_timeout_s: float = DEFAULT_POOL_TIMEOUT_S

    def __post_init__(self) -> None:
        if not self.database_url:
            raise ValueError("DATABASE_URL must not be empty")
        if self.pool_size <= 0:
            raise ValueError("POOL_SIZE must be greater than zero")
        if self.service_time_ms < 0:
            raise ValueError("SERVICE_TIME_MS must not be negative")
        if self.pool_timeout_s <= 0:
            raise ValueError("POOL_TIMEOUT_S must be greater than zero")

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        if "POOL_SIZE" not in os.environ:
            raise ValueError("POOL_SIZE is required")
        return cls(
            database_url=database_url,
            pool_size=int(os.environ["POOL_SIZE"]),
            service_time_ms=int(os.environ.get("SERVICE_TIME_MS", str(DEFAULT_SERVICE_TIME_MS))),
            pool_timeout_s=float(os.environ.get("POOL_TIMEOUT_S", str(DEFAULT_POOL_TIMEOUT_S))),
        )


@dataclass
class AppState:
    """Process-local counters and readiness. Single worker, single event loop."""

    ready: bool = False
    in_flight: int = 0
    ok: int = 0
    errors: int = 0


def create_app(
    settings: Settings | None = None, pool: AsyncConnectionPool | None = None
) -> FastAPI:
    """Build the app. ``pool`` may be injected (tests); otherwise one is built from settings."""
    cfg = settings if settings is not None else Settings.from_env()
    db_pool: AsyncConnectionPool = (
        pool
        if pool is not None
        else AsyncConnectionPool(
            conninfo=cfg.database_url,
            min_size=cfg.pool_size,
            max_size=cfg.pool_size,
            timeout=cfg.pool_timeout_s,
            max_waiting=0,
            open=False,
        )
    )
    state = AppState()
    service_s = cfg.service_time_ms / 1000

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await db_pool.open(wait=True)
        # One startup check. /healthz must NOT touch the pool afterwards: the compose
        # healthcheck runs every second and would steal pool capacity and inflate the pool
        # counters that the experiment measures.
        try:
            async with db_pool.connection() as conn:
                cur = await conn.execute("SELECT 1")
                await cur.fetchone()
            state.ready = True
        except Exception:
            logger.warning("startup check failed; staying not ready", exc_info=True)
        try:
            yield
        finally:
            state.ready = False
            await db_pool.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/work")
    async def work() -> JSONResponse:
        state.in_flight += 1
        try:
            async with db_pool.connection() as conn:
                await conn.execute("SELECT pg_sleep(%s)", (service_s,))
        except Exception:
            state.errors += 1
            logger.warning("work request failed", exc_info=True)
            return JSONResponse({"error": "unavailable"}, status_code=503)
        else:
            state.ok += 1
            return JSONResponse({"status": "ok"})
        finally:
            state.in_flight -= 1

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        if not state.ready:
            return JSONResponse({"status": "not ready"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:
        body: dict[str, Any] = dict(db_pool.get_stats())
        # psycopg_pool omits counters it has not touched yet (e.g. nothing has queued at low
        # load). Fill them with 0 so the metrics contract is always complete.
        for key in POOL_COUNTERS:
            body.setdefault(key, 0)
        body["in_flight"] = state.in_flight
        body["ok"] = state.ok
        body["errors"] = state.errors
        return body

    return app
